import type { AgentTurnFeedback } from "../../src/domain/feedback.js";

declare const feedbackBase: Pick<
  AgentTurnFeedback,
  | "session_id"
  | "turn_id"
  | "command_id"
  | "run_id"
  | "message_key"
  | "message"
  | "evidence_refs"
  | "completed_at"
>;

const providerFeedback: AgentTurnFeedback = {
  ...feedbackBase,
  source: "provider",
  degraded: false,
  fallback_reason: null,
};

const fallbackFeedback: AgentTurnFeedback = {
  ...feedbackBase,
  source: "provider_fallback",
  degraded: true,
  fallback_reason: "MODEL_OUTPUT_INVALID",
};

// @ts-expect-error Degraded feedback cannot masquerade as provider output.
const mislabeledFallback: AgentTurnFeedback = {
  ...feedbackBase,
  source: "provider",
  degraded: true,
  fallback_reason: "MODEL_OUTPUT_INVALID",
};

// @ts-expect-error Normal provider feedback cannot carry a fallback reason.
const providerWithReason: AgentTurnFeedback = {
  ...feedbackBase,
  source: "provider",
  degraded: false,
  fallback_reason: "MODEL_OUTPUT_INVALID",
};

void providerFeedback;
void fallbackFeedback;
void mislabeledFallback;
void providerWithReason;
