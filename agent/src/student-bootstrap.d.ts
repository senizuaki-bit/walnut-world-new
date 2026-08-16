import type {
  ActorRef,
  ContentRef,
  ISODateTime,
  LearnerId,
  RequestContext,
  Sha256,
  SkillId,
  SkillVersionId,
  WorldId,
} from "./domain/primitives.js";

export interface StudentBootstrapCapabilities {
  readonly skill_builds: boolean;
  readonly skill_activations: boolean;
  readonly agent_sessions: boolean;
  readonly http_world_recovery: boolean;
  readonly evidence_query: boolean;
}

export interface StudentSessionCreateRequest {
  readonly world_id: WorldId;
  readonly learner_id: LearnerId;
  readonly agent_profile_id: string;
  readonly channel: "GAME";
  readonly locale: string;
  readonly content: ContentRef;
  readonly expected_world_revision: number;
}

export interface StudentBootstrapSession {
  readonly current_session_id: string | null;
  readonly teaching_spec_version: string;
  readonly create_request: StudentSessionCreateRequest;
}

export type StudentSkillCapability =
  | "WORLD_READ"
  | "MOVE"
  | "PLANT"
  | "WATER"
  | "HARVEST"
  | "INTERACT"
  | "SPEAK";

export interface StudentBootstrapBuild {
  readonly build_policy_id: string;
  readonly compiler_profile: string;
  readonly compiler_version: string;
  readonly sandbox_image_digest: `sha256:${string}`;
  readonly test_suite_version: string;
  readonly allowed_capabilities: readonly StudentSkillCapability[];
  readonly max_source_files: 32;
  readonly max_source_bytes: 1048576;
}

export interface StudentActivationScope {
  readonly world_id: WorldId;
  readonly agent_profile_id: string;
}

export interface StudentActiveSkillAuthority {
  readonly activation_id: string;
  readonly skill_id: SkillId;
  readonly skill_version_id: SkillVersionId;
  readonly artifact_sha256: Sha256;
  readonly certification_id: string;
  readonly registry_revision: number;
  readonly activated_at: ISODateTime;
}

export interface StudentBootstrapActivation {
  readonly scope: StudentActivationScope;
  readonly registry_revision: number;
  readonly active: StudentActiveSkillAuthority | null;
}

export interface StudentBootstrapWorld {
  readonly world_id: WorldId;
  readonly revision: number;
  readonly last_event_sequence: number;
  readonly state_hash: Sha256;
  readonly snapshot_url: string;
  readonly events_url: string;
}

/** Closed v0.4 launch authority returned by GET /v1/student-bootstrap. */
export interface StudentBootstrapV2 {
  readonly request_context: RequestContext;
  readonly api_version: "1.1.0";
  readonly contract_version: "0.4.0";
  readonly server_time: ISODateTime;
  readonly actor: ActorRef;
  readonly content: ContentRef;
  readonly capabilities: StudentBootstrapCapabilities;
  readonly session: StudentBootstrapSession;
  readonly build: StudentBootstrapBuild;
  readonly activation: StudentBootstrapActivation;
  readonly world: StudentBootstrapWorld;
}
