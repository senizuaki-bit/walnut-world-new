import type {
  WorldAtomicCommit,
  WorldAtomicCommitReceipt,
} from "../domain/world.js";
import type { OperationContext } from "../domain/primitives.js";
import type { AsyncResult, ContractError } from "../domain/result.js";

/** The only world-write boundary; receipt.stream_id must equal request.stream_id. */
export interface WorldUnitOfWorkPort {
  commit(
    request: WorldAtomicCommit,
    context: OperationContext,
  ): AsyncResult<WorldAtomicCommitReceipt, ContractError>;
}
