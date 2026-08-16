import type { OperationContext, RunId } from "../domain/primitives.js";
import type { AsyncResult, ContractError } from "../domain/result.js";
import type {
  CompileAndTestRequest,
  CompileAndTestResult,
  SandboxRunRequest,
  SandboxRunResult,
} from "../domain/sandbox.js";

export interface SandboxCancellationError extends ContractError {
  readonly category: "SANDBOX" | "DEPENDENCY" | "INVARIANT";
}

/** Sandbox has no world-write capability; it can only return ActionIntent values. */
export interface SandboxPort {
  compileAndTest(
    request: CompileAndTestRequest,
    context: OperationContext,
  ): AsyncResult<CompileAndTestResult, ContractError>;

  run(
    request: SandboxRunRequest,
    context: OperationContext,
  ): AsyncResult<SandboxRunResult, ContractError>;

  cancel(
    runId: RunId,
    reasonCode: string,
    context: OperationContext,
  ): AsyncResult<void, ContractError>;
}
