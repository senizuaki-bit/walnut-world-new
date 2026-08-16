import type {
  DeliveryOperation,
  DeliveryPayload,
  DeliveryReceipt,
} from "./feishu.js";
import type { ISODateTime, OperationContext, Sha256 } from "./primitives.js";
import type { ContractError } from "./result.js";

export type OutboxStatus = "PENDING" | "SENDING" | "SENT" | "RETRYING" | "DEAD_LETTER";

/**
 * A tenant-level service delivery. operation_context.actor is the immutable
 * audit origin, but outbound deduplication intentionally uses only
 * tenant + destination + idempotency_key + payload hash.
 */
interface OutboxMessageBase {
  readonly message_id: string;
  readonly destination: DeliveryOperation;
  readonly idempotency_key: string;
  readonly payload: DeliveryPayload;
  readonly payload_sha256: Sha256;
  readonly created_at: ISODateTime;
  readonly operation_context: OperationContext;
}

export type OutboxMessage = OutboxMessageBase & (
  | {
      readonly status: "PENDING";
      readonly attempt: 0;
      readonly next_attempt_at: null;
      readonly lease_id: null;
      readonly lease_expires_at: null;
      readonly last_error: null;
      readonly delivery_receipt: null;
      readonly dead_lettered_at: null;
    }
  | {
      readonly status: "SENDING";
      readonly attempt: number;
      readonly next_attempt_at: null;
      readonly lease_id: string;
      readonly lease_expires_at: ISODateTime;
      readonly last_error: null;
      readonly delivery_receipt: null;
      readonly dead_lettered_at: null;
    }
  | {
      readonly status: "SENT";
      readonly attempt: number;
      readonly next_attempt_at: null;
      readonly lease_id: null;
      readonly lease_expires_at: null;
      readonly last_error: null;
      readonly delivery_receipt: DeliveryReceipt;
      readonly dead_lettered_at: null;
    }
  | {
      readonly status: "RETRYING";
      readonly attempt: number;
      readonly next_attempt_at: ISODateTime;
      readonly lease_id: null;
      readonly lease_expires_at: null;
      readonly last_error: ContractError;
      readonly delivery_receipt: null;
      readonly dead_lettered_at: null;
    }
  | {
      readonly status: "DEAD_LETTER";
      readonly attempt: number;
      readonly next_attempt_at: null;
      readonly lease_id: null;
      readonly lease_expires_at: null;
      readonly last_error: ContractError;
      readonly delivery_receipt: null;
      readonly dead_lettered_at: ISODateTime;
    }
);
