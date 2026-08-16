import type {
  DomainEvent,
  RuntimeEvent,
  UncommittedEvent,
} from "../../src/domain/events.js";
import type {
  JsonObject,
  OperationContext,
  StreamId,
} from "../../src/domain/primitives.js";
import type { EventStorePort } from "../../src/ports/event-store.port.js";

interface CustomDomainPayload extends JsonObject {
  readonly custom_value: string;
}

declare const customEvent: DomainEvent<"custom.domain_happened", CustomDomainPayload>;
declare const runtimeEvent: RuntimeEvent;
declare const eventStore: EventStorePort;
declare const streamId: StreamId;
declare const context: OperationContext;
declare const uncommittedEvent: UncommittedEvent<
  "custom.domain_happened",
  CustomDomainPayload
>;

// Generic domain streams accept event types that are not on the closed runtime bus.
const openDomainEvent: DomainEvent = customEvent;

// Every closed runtime-bus event is still a persisted domain event.
const persistedRuntimeEvent: DomainEvent = runtimeEvent;

// The generic store accepts an open event and receives stream identity separately.
const appendResult = eventStore.append(
  streamId,
  "NO_STREAM",
  [uncommittedEvent],
  context,
);

// @ts-expect-error An arbitrary domain event is not a closed runtime-bus event.
const invalidRuntimeEvent: RuntimeEvent = customEvent;

// @ts-expect-error stream_id is supplied once, by EventStorePort.append.
uncommittedEvent.stream_id;

// @ts-expect-error Store-assigned identity is unavailable before persistence.
uncommittedEvent.event_id;

void openDomainEvent;
void persistedRuntimeEvent;
void appendResult;
void invalidRuntimeEvent;
