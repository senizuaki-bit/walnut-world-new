import type { DeliveryReceipt } from "../domain/feishu.js";
import type { OutboxMessage } from "../domain/outbox.js";
import type { ISODateTime, OperationContext } from "../domain/primitives.js";
import type { AsyncResult, ContractError } from "../domain/result.js";

/**
 * Durable service-delivery state machine. Enqueue is idempotent by
 * tenant+destination+key+payload hash. The origin actor remains audit data and
 * is intentionally excluded because the delivery receipt is not actor-owned.
 */
export interface OutboxPort {
  enqueue(
    message: OutboxMessage,
    context: OperationContext,
  ): AsyncResult<OutboxMessage, ContractError>;

  claimReady(
    workerId: string,
    limit: number,
    leaseSeconds: number,
    context: OperationContext,
  ): AsyncResult<readonly OutboxMessage[], ContractError>;

  markSent(
    messageId: string,
    leaseId: string,
    receipt: DeliveryReceipt,
    context: OperationContext,
  ): AsyncResult<OutboxMessage, ContractError>;

  markRetry(
    messageId: string,
    leaseId: string,
    error: ContractError,
    nextAttemptAt: ISODateTime,
    context: OperationContext,
  ): AsyncResult<OutboxMessage, ContractError>;

  markDeadLetter(
    messageId: string,
    leaseId: string,
    error: ContractError,
    deadLetteredAt: ISODateTime,
    context: OperationContext,
  ): AsyncResult<OutboxMessage, ContractError>;
}
