import type { OperationContext, WorldId } from "../domain/primitives.js";
import type { AsyncResult, ContractError } from "../domain/result.js";
import type {
  WorldSnapshot,
} from "../domain/world.js";

export interface WorldReadError extends ContractError {
  readonly category: "VALIDATION" | "DEPENDENCY" | "INVARIANT";
}

export interface WorldPort {
  /** Read-only repository; all world writes go through WorldUnitOfWorkPort. */
  getSnapshot(
    worldId: WorldId,
    context: OperationContext,
  ): AsyncResult<WorldSnapshot, ContractError>;

}
