import type {
  CommandRecord,
  CommandTransition,
  NewCommand,
} from "../domain/commands.js";
import type {
  CommandId,
  CursorPage,
  ISODateTime,
  OperationContext,
} from "../domain/primitives.js";
import type { AsyncResult, ContractError } from "../domain/result.js";

export interface CommandCreateReceipt {
  readonly command: CommandRecord;
  /** False means the exact idempotent request already existed. */
  readonly created: boolean;
}

export interface CommandStoreError extends ContractError {
  readonly category: "CONCURRENCY" | "VALIDATION" | "DEPENDENCY" | "INVARIANT";
}

/** Every lookup and replay is bounded by the authenticated actor in OperationContext. */
export interface CommandStorePort {
  /**
   * Scope is tenant + actor + operation + key. Same scope + same request hash
   * returns created=false; same scope + different hash is CONCURRENCY.
   */
  acceptOnce(
    command: NewCommand,
    context: OperationContext,
  ): AsyncResult<CommandCreateReceipt, ContractError>;

  get(
    commandId: CommandId,
    context: OperationContext,
  ): AsyncResult<CommandRecord, ContractError>;

  getByIdempotencyKey(
    operation: CommandRecord["command_type"],
    idempotencyKey: string,
    context: OperationContext,
  ): AsyncResult<CommandRecord, ContractError>;

  transition(
    transition: CommandTransition,
    context: OperationContext,
  ): AsyncResult<CommandRecord, ContractError>;

  findNonTerminalBefore(
    updatedBefore: ISODateTime,
    cursor: string | null,
    limit: number,
    context: OperationContext,
  ): AsyncResult<CursorPage<CommandRecord>, ContractError>;
}
