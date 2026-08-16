import type {
  CursorPage,
  EventId,
  OperationContext,
  StreamId,
} from "../domain/primitives.js";
import type {
  DomainEvent,
  EventAppendReceipt,
  UncommittedEvent,
} from "../domain/events.js";
import type { AsyncResult, ContractError } from "../domain/result.js";

export type ExpectedStreamSequence = number | "NO_STREAM";

export interface EventStoreError extends ContractError {
  readonly category: "CONCURRENCY" | "DEPENDENCY" | "INVARIANT" | "VALIDATION";
}

export interface EventStorePort {
  /** Generic stream append only; world+event+outbox atomicity belongs to WorldUnitOfWorkPort. */
  append(
    streamId: StreamId,
    expectedSequence: ExpectedStreamSequence,
    events: readonly UncommittedEvent[],
    context: OperationContext,
  ): AsyncResult<EventAppendReceipt, ContractError>;

  readStream(
    streamId: StreamId,
    afterSequence: number,
    limit: number,
    context: OperationContext,
  ): AsyncResult<CursorPage<DomainEvent>, ContractError>;

  getById(
    eventId: EventId,
    context: OperationContext,
  ): AsyncResult<DomainEvent, ContractError>;
}
