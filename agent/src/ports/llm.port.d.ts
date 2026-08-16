import type { LLMRequest, LLMResponse } from "../domain/llm.js";
import type { JsonObject, OperationContext } from "../domain/primitives.js";
import type { AsyncResult, ContractError } from "../domain/result.js";

export interface LLMPort {
  /** Output must pass request.output_schema before an ok result is returned. */
  generate<Output extends JsonObject>(
    request: LLMRequest,
    context: OperationContext,
  ): AsyncResult<LLMResponse<Output>, ContractError>;
}
