import type { ISODateTime } from "./primitives.js";
import type { ContractError } from "./result.js";

export type DeliveryOperation = "FEISHU_REPORT_DRAFT";

export interface FeishuReportDraftBody {
  readonly report_id: string;
}

interface DeliveryPayloadBase {
  readonly delivery_id: string;
  readonly deduplication_key: string;
  readonly attempt: number;
}

export interface FeishuReportDraftDeliveryPayload extends DeliveryPayloadBase {
  readonly operation: "FEISHU_REPORT_DRAFT";
  readonly body: FeishuReportDraftBody;
}

/** Closed operation/body union consumed at the provider-neutral delivery boundary. */
export type DeliveryPayload = FeishuReportDraftDeliveryPayload;

export interface DeliveryReceipt {
  readonly delivery_id: string;
  readonly operation: DeliveryOperation;
  readonly deduplication_key: string;
  readonly report_id: string;
  readonly remote_object_id: string;
  readonly sent_at: ISODateTime;
  readonly attempt: number;
  readonly status: "SENT";
}

export type DeliveryFailure = ContractError;

/** @deprecated Compatibility aliases; provider-neutral names are authoritative. */
export type FeishuSyncRequest = DeliveryPayload;
export type FeishuSyncReceipt = DeliveryReceipt;
export type FeishuSyncFailure = DeliveryFailure;
