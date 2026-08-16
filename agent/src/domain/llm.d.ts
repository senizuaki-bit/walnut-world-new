import type { EvidenceRef, JsonObject, VersionSet } from "./primitives.js";
import type { ContractError } from "./result.js";

export interface LLMMessage {
  readonly role: "system" | "user" | "assistant" | "tool";
  readonly content: string;
  readonly name?: string;
  readonly tool_call_id?: string;
}

export interface LLMRequest {
  readonly messages: readonly LLMMessage[];
  readonly output_schema: JsonObject;
  readonly temperature: number;
  readonly max_output_tokens: number;
  readonly timeout_ms: number;
  readonly versions: VersionSet;
}

interface LLMResponseBase<Output extends JsonObject> {
  readonly output: Output;
  readonly provider: string;
  readonly model: string;
  readonly input_tokens: number;
  readonly output_tokens: number;
  readonly evidence_refs: readonly EvidenceRef[];
}

export type LLMResponseSource = "provider" | "provider_fallback";

export type LLMResponse<Output extends JsonObject = JsonObject> = LLMResponseBase<Output> & (
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

export interface LLMError extends ContractError {
  readonly category: "VALIDATION" | "DEPENDENCY" | "RATE_LIMIT" | "INTERNAL";
}
