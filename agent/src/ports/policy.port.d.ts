import type { OperationContext } from "../domain/primitives.js";
import type { PolicyGrant, PolicyInput } from "../domain/policy.js";
import type { AsyncResult, ContractError } from "../domain/result.js";

/** Authorization boundary. A grant is short-lived and scoped to one exact request. */
export interface PolicyPort {
  authorize(
    input: PolicyInput,
    context: OperationContext,
  ): AsyncResult<PolicyGrant, ContractError>;
}
