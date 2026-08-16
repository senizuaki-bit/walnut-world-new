import type {
  RealtimeAckFrame,
  RealtimeClientFrame,
  RealtimeCloseCode,
  RealtimeProtocolVersion,
  RealtimeServerControlFrame,
  RealtimeSubscribeFrame,
} from "../../src/domain/realtime.js";
import type {
  EventId,
  RequestId,
  StreamId,
} from "../../src/domain/primitives.js";

declare const requestId: RequestId;
declare const streamId: StreamId;
declare const eventId: EventId;

const protocol: RealtimeProtocolVersion = "1.0.0";
// @ts-expect-error Unknown protocol versions must be rejected at compile time.
const unsupportedProtocol: RealtimeProtocolVersion = "2.0.0";

const subscribe: RealtimeSubscribeFrame = {
  frame_type: "subscribe",
  protocol_version: protocol,
  request_id: requestId,
  stream_id: streamId,
  after_sequence: 731,
};

const ack: RealtimeAckFrame = {
  frame_type: "ack",
  protocol_version: protocol,
  subscription_id: "sub_realtime_00000001",
  stream_id: streamId,
  sequence: 732,
  event_id: eventId,
};

const clientFrames: readonly RealtimeClientFrame[] = [subscribe, ack];
declare const serverControl: RealtimeServerControlFrame;
const validClose: RealtimeCloseCode = 4409;
// @ts-expect-error Unreserved close codes must not enter adapters.
const invalidClose: RealtimeCloseCode = 4999;

void unsupportedProtocol;
void clientFrames;
void serverControl;
void validClose;
void invalidClose;
