import type { LLMResponse } from "../../src/domain/llm.js";
import type { OutboxMessage } from "../../src/domain/outbox.js";
import type {
  CommandRecord,
  CommandTransition,
} from "../../src/domain/commands.js";
import type { CommandStageChangedPayload } from "../../src/domain/events.js";
import type { WorldCommand } from "../../src/domain/world.js";

declare const commonLlm: Pick<
  LLMResponse,
  "output" | "provider" | "model" | "input_tokens" | "output_tokens" | "evidence_refs"
>;

const providerReply: LLMResponse = {
  ...commonLlm,
  source: "provider",
  degraded: false,
  fallback_reason: null,
};

const fallbackReply: LLMResponse = {
  ...commonLlm,
  source: "provider_fallback",
  degraded: true,
  fallback_reason: "MODEL_OUTPUT_INVALID",
};

// @ts-expect-error A fallback cannot masquerade as a provider response.
const mislabeledFallback: LLMResponse = {
  ...commonLlm,
  source: "provider",
  degraded: true,
  fallback_reason: "MODEL_OUTPUT_INVALID",
};

declare const outboxBase: Pick<
  OutboxMessage,
  | "message_id"
  | "destination"
  | "idempotency_key"
  | "payload"
  | "payload_sha256"
  | "created_at"
  | "operation_context"
>;

const pending: OutboxMessage = {
  ...outboxBase,
  status: "PENDING",
  attempt: 0,
  next_attempt_at: null,
  lease_id: null,
  lease_expires_at: null,
  last_error: null,
  delivery_receipt: null,
  dead_lettered_at: null,
};

// @ts-expect-error PENDING cannot carry a delivery attempt.
const contradictoryPending: OutboxMessage = {
  ...pending,
  attempt: 1,
};

void providerReply;
void fallbackReply;
void mislabeledFallback;
void pending;
void contradictoryPending;

declare const worldCommandBase: Omit<WorldCommand, "intents">;

const emptyWorldCommand: WorldCommand = {
  ...worldCommandBase,
  // @ts-expect-error A world write cannot commit an empty action set.
  intents: [],
};

void emptyWorldCommand;

declare const acceptedCommand: CommandRecord & { readonly status: "ACCEPTED" };
declare const validatingCommand: CommandRecord & { readonly status: "VALIDATING" };
declare const appliedCommand: CommandRecord & { readonly status: "APPLIED" };

const legalCommandTransition: CommandTransition = {
  previous_record: acceptedCommand,
  next_record: validatingCommand,
};

// @ts-expect-error ACCEPTED cannot skip validation and transition directly to APPLIED.
const illegalCommandTransition: CommandTransition = {
  previous_record: acceptedCommand,
  next_record: appliedCommand,
};

const legalStatusChangedEvent: CommandStageChangedPayload = {
  from_status: "ACCEPTED",
  to_status: "VALIDATING",
  command_revision: 2,
  attempt: 1,
};

const noopStatusChangedEvent: CommandStageChangedPayload = {
  from_status: "ACCEPTED",
  // @ts-expect-error A status-changed event cannot publish an ACCEPTED -> ACCEPTED no-op.
  to_status: "ACCEPTED",
  command_revision: 2,
  attempt: 1,
};

void legalCommandTransition;
void illegalCommandTransition;
void legalStatusChangedEvent;
void noopStatusChangedEvent;
