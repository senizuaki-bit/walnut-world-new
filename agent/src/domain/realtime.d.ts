import type {
  EventId,
  ISODateTime,
  JsonObject,
  RequestId,
  StreamId,
  WorldId,
} from "./primitives.js";
import type { DomainEvent } from "./events.js";
import type { ContractError } from "./result.js";

/** Wire version negotiated by bootstrap, upgrade headers and every control frame. */
export type RealtimeProtocolVersion = "1.0.0";

/** Application WebSocket close codes reserved by the realtime contract. */
export type RealtimeCloseCode =
  | 4400
  | 4401
  | 4403
  | 4404
  | 4406
  | 4408
  | 4409
  | 4429
  | 4500
  | 4503;

/** Realtime fields embedded in bootstrap.world. */
export interface RealtimeBootstrap extends JsonObject {
  readonly world_id: WorldId;
  readonly stream_id: StreamId;
  readonly stream_url: string;
  readonly last_event_sequence: number;
  readonly stream_protocol_version: RealtimeProtocolVersion;
}

export interface RealtimeSubscribeFrame extends JsonObject {
  readonly frame_type: "subscribe";
  readonly protocol_version: RealtimeProtocolVersion;
  readonly request_id: RequestId;
  readonly stream_id: StreamId;
  /** Replay begins at after_sequence + 1. */
  readonly after_sequence: number;
}

export interface RealtimeResumeFrame extends JsonObject {
  readonly frame_type: "resume";
  readonly protocol_version: RealtimeProtocolVersion;
  readonly request_id: RequestId;
  readonly subscription_id: string;
  readonly stream_id: StreamId;
  /** The highest contiguous sequence durably applied by the client. */
  readonly after_sequence: number;
}

export interface RealtimeAckFrame extends JsonObject {
  readonly frame_type: "ack";
  readonly protocol_version: RealtimeProtocolVersion;
  readonly subscription_id: string;
  readonly stream_id: StreamId;
  /** Highest contiguous sequence durably applied; never acknowledge across a gap. */
  readonly sequence: number;
  /** Event at sequence, used to detect a conflicting checkpoint. */
  readonly event_id: EventId;
}

export interface RealtimeHeartbeatAckFrame extends JsonObject {
  readonly frame_type: "heartbeat_ack";
  readonly protocol_version: RealtimeProtocolVersion;
  readonly subscription_id: string;
  readonly nonce: string;
  readonly received_at: ISODateTime;
}

export interface RealtimeSubscribedFrame extends JsonObject {
  readonly frame_type: "subscribed";
  readonly protocol_version: RealtimeProtocolVersion;
  readonly request_id: RequestId;
  readonly subscription_id: string;
  readonly stream_id: StreamId;
  readonly accepted_after_sequence: number;
  readonly high_watermark_sequence: number;
  readonly heartbeat_interval_ms: number;
  readonly max_unacked_events: number;
}

export interface RealtimeHeartbeatFrame extends JsonObject {
  readonly frame_type: "heartbeat";
  readonly protocol_version: RealtimeProtocolVersion;
  readonly subscription_id: string;
  readonly stream_id: StreamId;
  readonly nonce: string;
  readonly server_time: ISODateTime;
  readonly high_watermark_sequence: number;
}

export interface RealtimeErrorFrame extends JsonObject {
  readonly frame_type: "error";
  readonly protocol_version: RealtimeProtocolVersion;
  readonly request_id: RequestId | null;
  readonly subscription_id: string | null;
  readonly stream_id: StreamId | null;
  readonly fatal: boolean;
  /** Non-null exactly when fatal is true. */
  readonly close_code: RealtimeCloseCode | null;
  readonly retry_after_ms: number | null;
  readonly error: ContractError;
}

export type RealtimeClientFrame =
  | RealtimeSubscribeFrame
  | RealtimeResumeFrame
  | RealtimeAckFrame
  | RealtimeHeartbeatAckFrame;

export type RealtimeServerControlFrame =
  | RealtimeSubscribedFrame
  | RealtimeHeartbeatFrame
  | RealtimeErrorFrame;

/**
 * The live event is deliberately the same open DomainEvent shape returned in
 * world-event-page.events. RuntimeEvent remains the separate closed internal
 * integration-bus union.
 */
export type RealtimeWorldEvent = DomainEvent;

export type RealtimeServerFrame = RealtimeWorldEvent | RealtimeServerControlFrame;

/** Local durable state; a consumer persists this only after projection commit. */
export interface RealtimeCheckpoint {
  readonly stream_id: StreamId;
  readonly last_applied_sequence: number;
  readonly last_event_id: EventId | null;
}
