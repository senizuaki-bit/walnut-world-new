import type {
  DeliveryOperation,
  DeliveryPayload,
  DeliveryReceipt,
} from "../../src/domain/feishu.js";
import type { OutboxMessage } from "../../src/domain/outbox.js";

const operation: DeliveryOperation = "FEISHU_REPORT_DRAFT";

const valid: DeliveryPayload = {
  delivery_id: "delivery_0001",
  operation,
  deduplication_key: "delivery:req_ts_0001",
  attempt: 1,
  body: { report_id: "report_0001" },
};

const unknownOperation: DeliveryPayload = {
  ...valid,
  // @ts-expect-error Delivery operations are a closed discriminant.
  operation: "EMAIL_REPORT_DRAFT",
};

const missingReportId: DeliveryPayload = {
  ...valid,
  // @ts-expect-error The report-draft body requires report_id.
  body: {},
};

const extraBodyField: DeliveryPayload = {
  ...valid,
  // @ts-expect-error The report-draft body is closed to known fields.
  body: { report_id: "report_0001", ignored_typo: true },
};

declare const receiptTransport: Pick<
  DeliveryReceipt,
  "delivery_id" | "remote_object_id" | "sent_at" | "attempt" | "status"
>;

// @ts-expect-error A receipt must echo immutable request identity.
const receiptWithoutRequestIdentity: DeliveryReceipt = {
  ...receiptTransport,
};

declare const validOutbox: OutboxMessage;

const emptyOutboxPayload: OutboxMessage = {
  ...validOutbox,
  // @ts-expect-error A formal outbox message must carry a closed DeliveryPayload.
  payload: {},
};

const arbitraryOutboxPayload: OutboxMessage = {
  ...validOutbox,
  // @ts-expect-error Report body alone is not a DeliveryPayload.
  payload: { report_id: "report_0001" },
};

void valid;
void unknownOperation;
void missingReportId;
void extraBodyField;
void receiptWithoutRequestIdentity;
void emptyOutboxPayload;
void arbitraryOutboxPayload;
