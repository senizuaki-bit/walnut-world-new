import type {
  CommandId,
  EvidenceRef,
  ISODateTime,
  JsonObject,
  RunId,
} from "./primitives.js";
import type { LLMResponseSource } from "./llm.js";

interface AgentTurnFeedbackBase extends JsonObject {
  /** Agent session owning the accepted turn. */
  readonly session_id: string;
  /** Client-supplied turn_id from the accepted turn request. */
  readonly turn_id: string;
  /** Must equal the command_id in the containing runtime event envelope. */
  readonly command_id: CommandId;
  /** Sandbox run for this turn, or null when no run was started. */
  readonly run_id: RunId | null;
  readonly message_key: string;
  /** Policy-filtered text that a game client may display directly. */
  readonly message: string;
  readonly evidence_refs: readonly EvidenceRef[];
  readonly completed_at: ISODateTime;
}

export type AgentTurnFeedbackSource = LLMResponseSource;

/**
 * Public feedback projection for one agent turn.
 *
 * This discriminated union makes it impossible for typed producers to silently
 * label a fallback as a normal provider response (or vice versa).
 */
export type AgentTurnFeedback = AgentTurnFeedbackBase & (
  | {
      readonly source: "provider";
      readonly degraded: false;
      readonly fallback_reason: null;
    }
  | {
      readonly source: "provider_fallback";
      readonly degraded: true;
      readonly fallback_reason: string;
    }
);
