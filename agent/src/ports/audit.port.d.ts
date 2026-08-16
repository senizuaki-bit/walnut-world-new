import type { AuditQuery, AuditRecord } from "../domain/audit.js";
import type { CursorPage, OperationContext } from "../domain/primitives.js";
import type { AsyncResult, ContractError } from "../domain/result.js";

/** Append-only redacted access audit boundary scoped by context.actor.tenant_id. */
export interface AuditPort {
  append(
    record: AuditRecord,
    context: OperationContext,
  ): AsyncResult<AuditRecord, ContractError>;

  query(
    query: AuditQuery,
    context: OperationContext,
  ): AsyncResult<CursorPage<AuditRecord>, ContractError>;
}
