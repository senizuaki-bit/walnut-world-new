import type {
  DeliveryFailure,
  DeliveryPayload,
  DeliveryReceipt,
} from "../domain/feishu.js";
import type { OperationContext } from "../domain/primitives.js";
import type { AsyncResult } from "../domain/result.js";

/** Provider-neutral external delivery adapter. Invoke only from a durable outbox worker. */
export interface DeliveryPort {
  deliver(
    payload: DeliveryPayload,
    context: OperationContext,
  ): AsyncResult<DeliveryReceipt, DeliveryFailure>;
}

/** @deprecated Prefer DeliveryPort; this compatibility alias is Feishu-specific. */
export type FeishuPort = DeliveryPort;
