import type {
  CommandId,
  ContentRef,
  EvidenceRef,
  ISODateTime,
  JsonObject,
  RequestContext,
  Sha256,
  VersionSet,
} from "./primitives.js";
import type { ContractError } from "./result.js";

export type CommandStatus =
  | "ACCEPTED"
  | "VALIDATING"
  | "RUNNING_SANDBOX"
  | "APPLYING_WORLD"
  | "APPLIED"
  | "REJECTED"
  | "FAILED"
  | "UNKNOWN"
  | "CANCELLED";

export type CommandType =
  | "CREATE_SKILL_BUILD"
  | "ACTIVATE_SKILL_VERSION"
  | "CREATE_AGENT_SESSION"
  | "EXECUTE_AGENT_TURN"
  | "INGEST_CLIENT_EVENTS";

export type TerminalCommandStatus =
  | "APPLIED"
  | "REJECTED"
  | "FAILED"
  | "UNKNOWN"
  | "CANCELLED";

export type MutableCommandStatus = Exclude<CommandStatus, TerminalCommandStatus>;

/** Closed state graph shared by stores and command lifecycle events. */
export type CommandStatusSuccessor<Status extends MutableCommandStatus> =
  Status extends "ACCEPTED"
    ? "VALIDATING" | "REJECTED" | "FAILED" | "CANCELLED"
    : Status extends "VALIDATING"
      ? "VALIDATING" | "RUNNING_SANDBOX" | "APPLYING_WORLD" | "APPLIED" | "REJECTED" | "FAILED" | "CANCELLED"
      : Status extends "RUNNING_SANDBOX"
        ? "APPLYING_WORLD" | "APPLIED" | "REJECTED" | "FAILED" | "CANCELLED"
        : Status extends "APPLYING_WORLD"
          ? "APPLYING_WORLD" | "APPLIED" | "REJECTED" | "FAILED" | "UNKNOWN" | "CANCELLED"
          : never;

export interface CommandRecord<ResultBody extends JsonObject = JsonObject> {
  readonly request_context: RequestContext;
  readonly command_id: CommandId;
  readonly revision: number;
  readonly command_type: CommandType;
  readonly status: CommandStatus;
  readonly stage:
    | "ACCEPT"
    | "VALIDATE"
    | "POLICY"
    | "REGISTRY"
    | "SANDBOX"
    | "WORLD_VALIDATE"
    | "WORLD_COMMIT"
    | "EVIDENCE"
    | "COMPLETE";
  readonly terminal: boolean;
  readonly accepted_at: ISODateTime;
  readonly updated_at: ISODateTime;
  readonly result: ResultBody | null;
  readonly error: ContractError | null;
  readonly evidence_refs: readonly EvidenceRef[];
  readonly versions: VersionSet;
  readonly links: {
    readonly self: string;
    readonly run?: string;
    readonly world_snapshot?: string;
  };
}

/**
 * Immutable command acceptance input. The store derives the idempotency scope
 * from context.actor.tenant_id + context.actor.actor_id + command_type + key;
 * actor identity is never duplicated in this value.
 */
export interface NewCommand {
  readonly command_type: CommandType;
  readonly idempotency_key: string;
  readonly request_sha256: Sha256;
  readonly versions: VersionSet;
}

export type CommandTransition<ResultBody extends JsonObject = JsonObject> = {
  readonly [Status in MutableCommandStatus]: {
    /** Persist only when the stored CAS key still matches this immutable snapshot. */
    readonly previous_record: CommandRecord<ResultBody> & { readonly status: Status };
    readonly next_record: CommandRecord<ResultBody> & {
      readonly status: CommandStatusSuccessor<Status>;
    };
  };
}[MutableCommandStatus];
