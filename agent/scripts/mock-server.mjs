import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import http from "node:http";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual } from "node:util";
import { canonicalJsonSha256V1 } from "../src/canonical-json.mjs";
import {
  assertSchema,
  loadDocuments,
} from "./validate-contracts.mjs";
import {
  assertAgentTurnFeedbackReadyEvent,
  assertClassInsightsPrivacy,
  assertClientEventBatch,
  assertEventSequenceRange,
  assertUniqueEvidenceRefs,
  assertWorldCommitEvidence,
  assertWorldRevisionAdvance,
  assertWorldEventPage,
  SemanticInvariantError,
} from "../src/semantic-invariants.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(SCRIPT_DIR, "..");
const DEFAULT_PORT = Number(process.env.YAYA_AGENT_MOCK_PORT ?? 8790);
const DEFAULT_FEISHU_SECRET = process.env.YAYA_FEISHU_MOCK_SECRET ?? "yaya-feishu-contract-mock-secret";
const MAX_HTTP_BODY_BYTES = 8 * 1024 * 1024;
const MAX_IDEMPOTENCY_RECORDS = 10_000;
const FEISHU_CLOCK_SKEW_SECONDS = 300;
const REQUEST_ID_PATTERN = /^req_[A-Za-z0-9_-]{8,96}$/u;
const TRACE_ID_PATTERN = /^trace_[A-Za-z0-9_-]{8,96}$/u;
const CORRELATION_ID_PATTERN = /^corr_[A-Za-z0-9_-]{8,96}$/u;
const FEISHU_TIMESTAMP_PATTERN = /^[0-9]{10,16}$/u;
const FIXTURE_TENANT_ID = "tenant_yaya";
const IDEMPOTENT_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const SUPPORTED_FEISHU_EVENTS = new Set(["approval_instance"]);
export const MOCK_FEISHU_ROLE_POLICIES = new Map([
  ["POST /integrations/feishu/v1/content-releases", "content:submitter"],
  ["GET /integrations/feishu/v1/content-releases/{release_id}", "content:read"],
  ["POST /integrations/feishu/v1/approval-decisions", "content:approver"],
  ["POST /integrations/feishu/v1/learner-queries", "learner:read"],
  ["POST /integrations/feishu/v1/class-insights", "class-insights:read"],
  ["POST /integrations/feishu/v1/report-jobs", "report:create"],
  ["GET /integrations/feishu/v1/report-jobs/{job_id}", "report:read"],
  ["GET /integrations/feishu/v1/evidence/{evidence_id}", "evidence:read"],
]);
const MOCK_OPERATOR_ROLES = new Set(MOCK_FEISHU_ROLE_POLICIES.values());
const MOCK_TEACHER_ROLES = new Set([
  "teacher",
  "content:submitter",
  "content:read",
  "learner:read",
  "class-insights:read",
  "report:create",
  "report:read",
  "evidence:read",
]);
const { documents: CONTRACT_DOCUMENTS } = loadDocuments();
const FEISHU_OPENAPI_PATH = resolve(PROJECT_ROOT, "contracts/openapi/feishu-integration.openapi.json");
const FEISHU_EVIDENCE_PURPOSES = new Set(
  CONTRACT_DOCUMENTS.get(FEISHU_OPENAPI_PATH).components.parameters.EvidencePurpose.schema.enum,
);

const SCHEMAS = {
  auditRecord: "contracts/schemas/common/audit-record.schema.json",
  acceptedGameJob: "contracts/schemas/game/accepted-game-job.schema.json",
  errorResponse: "contracts/schemas/common/error-response.schema.json",
  gameSkillBuildRequest: "contracts/schemas/game/skill-build-create-request.schema.json",
  gameSkillActivationRequest: "contracts/schemas/game/skill-activation-request.schema.json",
  gameAgentSessionRequest: "contracts/schemas/game/agent-session-create-request.schema.json",
  gameAgentTurnRequest: "contracts/schemas/game/agent-turn-create-request.schema.json",
  gameClientEventBatchRequest: "contracts/schemas/game/client-event-batch-request.schema.json",
  gameBootstrap: "contracts/schemas/game/bootstrap-response.schema.json",
  gameSkillBuild: "contracts/schemas/game/skill-build.schema.json",
  gameSkillActivation: "contracts/schemas/game/skill-activation.schema.json",
  gameAgentSession: "contracts/schemas/game/agent-session.schema.json",
  gameCommand: "contracts/schemas/game/command.schema.json",
  gameRun: "contracts/schemas/game/run.schema.json",
  gameSnapshot: "contracts/schemas/game/world-snapshot.schema.json",
  gameWorldEvents: "contracts/schemas/game/world-event-page.schema.json",
  gameEvidence: "contracts/schemas/game/evidence.schema.json",
  feishuWebhookRequest: "contracts/schemas/feishu/webhook.schema.json",
  feishuWebhookResponse: "contracts/schemas/feishu/webhook-response.schema.json",
  feishuReleaseRequest: "contracts/schemas/feishu/content-release.schema.json",
  feishuReleaseResponse: "contracts/schemas/feishu/content-release-receipt.schema.json",
  feishuReleaseStatus: "contracts/schemas/feishu/content-release-status.schema.json",
  feishuApprovalRequest: "contracts/schemas/feishu/approval-decision.schema.json",
  feishuApprovalResponse: "contracts/schemas/feishu/approval-decision-receipt.schema.json",
  feishuLearnerRequest: "contracts/schemas/feishu/learner-query.schema.json",
  feishuLearnerResponse: "contracts/schemas/feishu/learner-query-result.schema.json",
  feishuClassRequest: "contracts/schemas/feishu/class-insights-query.schema.json",
  feishuClassResponse: "contracts/schemas/feishu/class-insights-result.schema.json",
  feishuReportRequest: "contracts/schemas/feishu/report-job-request.schema.json",
  feishuReportResponse: "contracts/schemas/feishu/report-job.schema.json",
  feishuEvidence: "contracts/schemas/feishu/evidence-view.schema.json",
};

function readJson(path) {
  return JSON.parse(readFileSync(resolve(PROJECT_ROOT, path), "utf8"));
}

function example(name) {
  return readJson(`contracts/examples/${name}`).value;
}

const EXAMPLES = {
  skillBuildRequest: example("game-skill-build-create-request.json"),
  agentSessionRequest: example("game-agent-session-create-request.json"),
  agentTurnRequest: example("game-agent-turn-create-request.json"),
  clientEventBatchRequest: example("game-client-event-batch-request.json"),
  bootstrap: example("game-bootstrap-response.json"),
  skillBuild: example("game-skill-build.json"),
  skillActivation: example("game-skill-activation.json"),
  agentSession: example("game-agent-session.json"),
  command: example("game-command.json"),
  run: example("game-run.json"),
  snapshot: example("game-world-snapshot.json"),
  worldEvents: example("game-world-event-page.json"),
  evidence: example("game-evidence.json"),
  feishuWebhookRequest: example("feishu-webhook-event.json"),
  feishuRelease: example("feishu-content-release-response.json"),
  feishuReleaseStatus: example("feishu-content-release-status-response.json"),
  feishuApproval: example("feishu-approval-decision-response.json"),
  feishuLearner: example("feishu-learner-query-response.json"),
  feishuClass: example("feishu-class-insights-response.json"),
  feishuReport: example("feishu-report-job-response.json"),
  feishuEvidence: example("feishu-evidence-response.json"),
};
const SOURCE_LIMITS = Object.freeze({
  maxFiles: EXAMPLES.bootstrap.limits.max_source_files,
  maxBytes: EXAMPLES.bootstrap.limits.max_source_bytes,
});

const ERROR_CATALOG = new Map(
  readJson("contracts/error-catalog.json").errors.map((entry) => [entry.code, entry]),
);
const FEISHU_OPENAPI = CONTRACT_DOCUMENTS.get(FEISHU_OPENAPI_PATH);
const AUDITED_FEISHU_OPERATIONS = Object.entries(FEISHU_OPENAPI.paths).flatMap(([template, pathItem]) => (
  Object.entries(pathItem)
    .filter(([, operation]) => operation?.["x-audit-access"] === true)
    .map(([method, operation]) => ({
      method: method.toUpperCase(),
      template,
      operation: operation.operationId,
    }))
));

class HttpContractError extends Error {
  constructor(code, details = {}, statusOverride = undefined, headers = {}) {
    super(code);
    this.name = "HttpContractError";
    this.code = code;
    this.details = details;
    this.statusOverride = statusOverride;
    this.headers = headers;
  }
}

function schemaPath(key) {
  const relativePath = SCHEMAS[key];
  if (!relativePath) throw new Error(`Unknown contract schema key ${key}`);
  return resolve(PROJECT_ROOT, relativePath);
}

function validateValue(value, key, label = key, failureCode = "INVALID_REQUEST") {
  const path = schemaPath(key);
  const schema = CONTRACT_DOCUMENTS.get(path) ?? readJson(SCHEMAS[key]);
  try {
    assertSchema(value, schema, path, CONTRACT_DOCUMENTS, label);
  } catch (error) {
    throw new HttpContractError(failureCode, {
      contract: key,
      reason: error instanceof Error ? error.message : String(error),
    });
  }
}

function requestIdentity(request) {
  const suppliedRequestId = request.headers["x-request-id"];
  const requestId = typeof suppliedRequestId === "string" && REQUEST_ID_PATTERN.test(suppliedRequestId)
    ? suppliedRequestId
    : suppliedRequestId === undefined
      ? "req_mock_00000001"
      : `req_mock_${createHash("sha256").update(String(suppliedRequestId)).digest("hex").slice(0, 16)}`;
  const suppliedTraceId = request.headers["x-trace-id"];
  const traceId = typeof suppliedTraceId === "string" && TRACE_ID_PATTERN.test(suppliedTraceId)
    ? suppliedTraceId
    : `trace_${createHash("sha256").update(requestId).digest("hex").slice(0, 16)}`;
  const suppliedCorrelationId = request.headers["x-correlation-id"];
  const correlationId = typeof suppliedCorrelationId === "string"
      && CORRELATION_ID_PATTERN.test(suppliedCorrelationId)
    ? suppliedCorrelationId
    : `corr_${createHash("sha256").update(traceId).digest("hex").slice(0, 16)}`;
  return { requestId, traceId, correlationId };
}

function responseHeaders(request, extras = {}) {
  const { requestId, traceId, correlationId } = requestIdentity(request);
  return {
    "X-Request-Id": requestId,
    "X-Trace-Id": traceId,
    ...(String(request.url ?? "").startsWith("/v1/")
      ? { "X-Correlation-Id": correlationId }
      : {}),
    "X-Schema-Version": "1.0.0",
    ...extras,
  };
}

function sendJson(request, response, status, payload, headers = {}) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    ...responseHeaders(request),
    ...headers,
  });
  response.end(body);
}

function errorResponse(request, code, details = {}, statusOverride = undefined) {
  const catalog = ERROR_CATALOG.get(code) ?? ERROR_CATALOG.get("INTERNAL_ERROR");
  const { requestId, traceId } = requestIdentity(request);
  const payload = {
    request_id: requestId,
    trace_id: traceId,
    status: catalog.code === "UNKNOWN_COMMIT_STATE" ? "UNKNOWN" : "FAILED",
    data: null,
    error: {
      code: catalog.code,
      category: catalog.category,
      retryable: catalog.retryable,
      user_message_key: catalog.user_message_key,
      stage: details.stage ?? "HTTP_ADAPTER",
      details: Object.fromEntries(
        Object.entries(details).filter(([key]) => !["stage", "command_id"].includes(key)),
      ),
    },
  };
  if (catalog.code === "UNKNOWN_COMMIT_STATE"
    && typeof details.command_id === "string"
    && /^cmd_[A-Za-z0-9_-]{8,96}$/u.test(details.command_id)) {
    payload.command_id = details.command_id;
  }
  validateValue(payload, "errorResponse", "error response");
  const status = statusOverride ?? catalog.http_status;
  const requiresRetryAfter = status === 429 || (status === 503 && catalog.retryable === true);
  return {
    status,
    payload,
    headers: requiresRetryAfter ? { "Retry-After": "1" } : {},
  };
}

async function readRawBody(request) {
  const chunks = [];
  let size = 0;
  try {
    for await (const chunk of request) {
      size += chunk.length;
      if (size > MAX_HTTP_BODY_BYTES) {
        throw new HttpContractError("PAYLOAD_TOO_LARGE", {
          limit_scope: "HTTP_BODY",
          limit_bytes: MAX_HTTP_BODY_BYTES,
        });
      }
      chunks.push(chunk);
    }
  } catch (error) {
    if (error instanceof HttpContractError) throw error;
    throw new HttpContractError("INVALID_REQUEST", { reason: "REQUEST_STREAM_INTERRUPTED" });
  }
  return Buffer.concat(chunks);
}

function parseJsonBody(raw) {
  try {
    return raw.length === 0 ? {} : JSON.parse(raw.toString("utf8"));
  } catch {
    throw new HttpContractError("INVALID_REQUEST", { reason: "MALFORMED_JSON" });
  }
}

function requireHeader(request, name) {
  const value = request.headers[name.toLowerCase()];
  if (typeof value !== "string" || value.length === 0) {
    throw new HttpContractError("INVALID_REQUEST", { missing_header: name });
  }
  return value;
}

function validateCommonHeaders(request, pathname, method) {
  if (pathname === "/health") return;
  const requestId = requireHeader(request, "X-Request-Id");
  if (!REQUEST_ID_PATTERN.test(requestId)) {
    throw new HttpContractError("INVALID_REQUEST", { invalid_header: "X-Request-Id" });
  }
  const schemaVersion = requireHeader(request, "X-Schema-Version");
  if (schemaVersion !== "1.0.0") {
    throw new HttpContractError("SCHEMA_VERSION_UNSUPPORTED", { supplied: schemaVersion });
  }
  if (pathname.startsWith("/v1/") || pathname.startsWith("/integrations/feishu/")) {
    const traceId = requireHeader(request, "X-Trace-Id");
    if (!TRACE_ID_PATTERN.test(traceId)) {
      throw new HttpContractError("INVALID_REQUEST", { invalid_header: "X-Trace-Id" });
    }
  }
  if (pathname.startsWith("/v1/")) {
    const correlationId = requireHeader(request, "X-Correlation-Id");
    if (!CORRELATION_ID_PATTERN.test(correlationId)) {
      throw new HttpContractError("INVALID_REQUEST", { invalid_header: "X-Correlation-Id" });
    }
  }
  if (IDEMPOTENT_METHODS.has(method)) {
    const key = requireHeader(request, "Idempotency-Key");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u.test(key)) {
      throw new HttpContractError("INVALID_REQUEST", { invalid_header: "Idempotency-Key" });
    }
    const contentType = String(request.headers["content-type"] ?? "").split(";", 1)[0].trim().toLowerCase();
    if (contentType !== "application/json") {
      throw new HttpContractError("INVALID_REQUEST", { expected_content_type: "application/json" });
    }
  }
}

function mockPrincipal(request) {
  const authorization = request.headers.authorization;
  if (typeof authorization !== "string" || authorization.length === 0) {
    throw new HttpContractError("AUTHENTICATION_REQUIRED", { reason: "MISSING_MOCK_BEARER" });
  }
  const match = /^Bearer\s+([A-Za-z0-9_-]{3,96}):([A-Za-z0-9_-]{3,128})$/u.exec(authorization);
  if (!match) throw new HttpContractError("AUTHENTICATION_REQUIRED", { reason: "INVALID_MOCK_BEARER" });
  const actorId = match[2];
  if (actorId.startsWith("operator_") || actorId.startsWith("feishu_operator_")) {
    return { tenantId: match[1], actorId, actorType: "operator", roles: new Set([...MOCK_OPERATOR_ROLES, "operator"]) };
  }
  if (actorId.startsWith("feishu_teacher_")) {
    return { tenantId: match[1], actorId, actorType: "teacher", roles: new Set(MOCK_TEACHER_ROLES) };
  }
  return { tenantId: match[1], actorId, actorType: "student", roles: new Set(["game:player"]) };
}

function pathMatchesTemplate(pathname, template) {
  const actual = pathname.split("/");
  const expected = template.split("/");
  return actual.length === expected.length && expected.every((segment, index) => (
    /^\{[a-z][a-z0-9_]*\}$/u.test(segment) ? actual[index].length > 0 : segment === actual[index]
  ));
}

function auditedAccessOperation(method, pathname) {
  return AUDITED_FEISHU_OPERATIONS.find((candidate) => (
    candidate.method === method && pathMatchesTemplate(pathname, candidate.template)
  ));
}

function safeAuditIdentity(value, prefix) {
  const pattern = prefix === "req"
    ? /^req_[A-Za-z0-9_-]{8,96}$/u
    : /^trace_[A-Za-z0-9_-]{8,96}$/u;
  if (pattern.test(String(value))) return String(value);
  return `${prefix}_audit_${createHash("sha256").update(String(value)).digest("hex").slice(0, 16)}`;
}

function auditSubjectHash(tenantId, learnerRef) {
  if (typeof learnerRef !== "string" || learnerRef.length === 0) return null;
  return createHash("sha256").update(`${tenantId}:${learnerRef}`).digest("hex");
}

function auditResource(operation, pathname, body, principal) {
  if (operation === "queryLearnerProjectionFromFeishu") {
    const subjectHash = auditSubjectHash(principal.tenantId, body?.learner_ref);
    return {
      resourceType: "LEARNER_PROJECTION",
      resourceId: subjectHash ? `learner_projection_${subjectHash.slice(0, 16)}` : "learner_projection_unknown",
      subjectHash,
      evidenceIds: [],
    };
  }
  if (operation === "queryClassInsightsFromFeishu") {
    const classHash = typeof body?.class_ref === "string"
      ? createHash("sha256").update(`${principal.tenantId}:${body.class_ref}`).digest("hex")
      : undefined;
    return {
      resourceType: "CLASS_INSIGHTS",
      resourceId: classHash ? `class_insights_${classHash.slice(0, 16)}` : "class_insights_unknown",
      subjectHash: null,
      evidenceIds: [],
    };
  }
  const encodedEvidenceId = pathname.split("/").at(-1) ?? "";
  let evidenceId;
  try {
    evidenceId = decodeURIComponent(encodedEvidenceId);
  } catch {
    evidenceId = "evidence_unknown";
  }
  const validEvidenceId = /^evidence_[A-Za-z0-9_-]{8,128}$/u.test(evidenceId);
  const isFixtureEvidence = evidenceId === EXAMPLES.feishuEvidence.evidence_ref.evidence_id;
  return {
    resourceType: "EVIDENCE",
    resourceId: validEvidenceId ? evidenceId : "evidence_unknown",
    subjectHash: isFixtureEvidence
      ? auditSubjectHash(principal.tenantId, EXAMPLES.feishuEvidence.learner_ref)
      : null,
    evidenceIds: validEvidenceId ? [evidenceId] : [],
  };
}

function auditOutcomeForError(code) {
  const category = ERROR_CATALOG.get(code)?.category;
  return ["AUTHENTICATION", "AUTHORIZATION", "POLICY"].includes(category) ? "DENIED" : "FAILED";
}

function appendAuditRecord(state, auditAccess, request, url, body, principal, outcome, errorCode, status, nowMs) {
  if (!auditAccess) return;
  const rawIdentity = requestIdentity(request);
  const requestId = safeAuditIdentity(rawIdentity.requestId, "req");
  const traceId = safeAuditIdentity(rawIdentity.traceId, "trace");
  const safePrincipal = principal?.actorType
    ? principal
    : { tenantId: "tenant_unknown", actorId: "actor_unknown", actorType: "service", roles: new Set() };
  const resource = auditResource(auditAccess.operation, url.pathname, body, safePrincipal);
  const suppliedCorrelationId = body?.context?.correlation_id;
  const correlationId = /^corr_[A-Za-z0-9_-]{8,96}$/u.test(String(suppliedCorrelationId))
    ? suppliedCorrelationId
    : `corr_audit_${createHash("sha256").update(requestId).digest("hex").slice(0, 16)}`;
  const suppliedPurpose = auditAccess.operation === "getRedactedEvidenceForFeishu"
    ? url.searchParams.getAll("purpose")[0]
    : body?.purpose;
  const purpose = /^[A-Z][A-Z0-9_]{2,63}$/u.test(String(suppliedPurpose)) ? suppliedPurpose : null;
  const auditIdSeed = `${requestId}:${traceId}:${auditAccess.operation}:${state.auditRecords.length}`;
  const record = {
    schema_version: "1.0.0",
    audit_id: `audit_${createHash("sha256").update(auditIdSeed).digest("hex").slice(0, 24)}`,
    occurred_at: new Date(nowMs).toISOString(),
    operation: auditAccess.operation,
    outcome,
    actor: {
      tenant_id: safePrincipal.tenantId,
      actor_id: safePrincipal.actorId,
      actor_type: safePrincipal.actorType,
      roles: [...safePrincipal.roles].sort(),
    },
    request_id: requestId,
    correlation_id: correlationId,
    trace_id: traceId,
    resource_type: resource.resourceType,
    resource_id: resource.resourceId,
    purpose,
    subject_hash: resource.subjectHash,
    redacted: true,
    evidence_ids: resource.evidenceIds,
    error_code: errorCode,
    details: { http_status: status },
  };
  validateValue(record, "auditRecord", `${auditAccess.operation} audit record`, "INTERNAL_ERROR");
  state.auditRecords.push(record);
}

function enforceFeishuRole(principal, method, pathname) {
  if (!pathname.startsWith("/integrations/feishu/") || pathname.endsWith("/webhooks")) return;
  for (const [operation, requiredRole] of MOCK_FEISHU_ROLE_POLICIES) {
    const separator = operation.indexOf(" ");
    const policyMethod = operation.slice(0, separator);
    const template = operation.slice(separator + 1);
    if (method === policyMethod && pathMatchesTemplate(pathname, template)) {
      if (!principal.roles.has(requiredRole)) {
        throw new HttpContractError("AUTHORIZATION_DENIED", { required_role: requiredRole });
      }
      return;
    }
  }
}

function requestSchema(method, pathname) {
  if (method !== "POST") return undefined;
  if (pathname === "/v1/skill-builds") return "gameSkillBuildRequest";
  if (/^\/v1\/skill-versions\/[^/]+\/activations$/u.test(pathname)) return "gameSkillActivationRequest";
  if (pathname === "/v1/agent-sessions") return "gameAgentSessionRequest";
  if (/^\/v1\/agent-sessions\/[^/]+\/turns$/u.test(pathname)) return "gameAgentTurnRequest";
  if (pathname === "/v1/client-events:batch") return "gameClientEventBatchRequest";
  if (pathname === "/integrations/feishu/v1/webhooks") return "feishuWebhookRequest";
  if (pathname === "/integrations/feishu/v1/content-releases") return "feishuReleaseRequest";
  if (pathname === "/integrations/feishu/v1/approval-decisions") return "feishuApprovalRequest";
  if (pathname === "/integrations/feishu/v1/learner-queries") return "feishuLearnerRequest";
  if (pathname === "/integrations/feishu/v1/class-insights") return "feishuClassRequest";
  if (pathname === "/integrations/feishu/v1/report-jobs") return "feishuReportRequest";
  return undefined;
}

function operationScope(method, pathname) {
  if (method === "POST" && pathname === "/v1/skill-builds") return "createSkillBuild";
  if (method === "POST" && /^\/v1\/skill-versions\/[^/]+\/activations$/u.test(pathname)) return "activateSkillVersion";
  if (method === "POST" && pathname === "/v1/agent-sessions") return "createAgentSession";
  if (method === "POST" && /^\/v1\/agent-sessions\/[^/]+\/turns$/u.test(pathname)) return "createAgentTurn";
  if (method === "POST" && pathname === "/v1/client-events:batch") return "ingestClientEventBatch";
  if (method === "POST" && pathname === "/integrations/feishu/v1/content-releases") return "createFeishuContentReleaseCandidate";
  if (method === "POST" && pathname === "/integrations/feishu/v1/approval-decisions") return "recordFeishuApprovalDecision";
  if (method === "POST" && pathname === "/integrations/feishu/v1/learner-queries") return "queryLearnerProjectionFromFeishu";
  if (method === "POST" && pathname === "/integrations/feishu/v1/class-insights") return "queryClassInsightsFromFeishu";
  if (method === "POST" && pathname === "/integrations/feishu/v1/report-jobs") return "createFeishuReportDraftJob";
  return `${method}:${pathname}`;
}

function validateRequestSemantics(key, body) {
  if (key === "gameSkillBuildRequest") {
    validateSkillSourceBundle(body.source_bundle);
  } else if (key === "gameClientEventBatchRequest") {
    enforceSemantic(() => assertClientEventBatch(body));
  }
}

function validateSkillSourceBundle(bundle) {
  if (bundle.files.length > SOURCE_LIMITS.maxFiles) {
    throw new HttpContractError("INVALID_REQUEST", {
      reason: "SOURCE_FILE_LIMIT_EXCEEDED",
      file_count: bundle.files.length,
      max_source_files: SOURCE_LIMITS.maxFiles,
    });
  }
  const seenPaths = new Set();
  let entrypointMatches = 0;
  let totalSourceBytes = 0;
  for (const file of bundle.files) {
    if (seenPaths.has(file.path)) {
      throw new HttpContractError("INVALID_REQUEST", {
        reason: "DUPLICATE_SOURCE_PATH",
        path: file.path,
      });
    }
    seenPaths.add(file.path);
    if (file.path === bundle.entrypoint) entrypointMatches += 1;
    totalSourceBytes += Buffer.byteLength(file.content, "utf8");
    if (totalSourceBytes > SOURCE_LIMITS.maxBytes) {
      throw new HttpContractError("INVALID_REQUEST", {
        reason: "SOURCE_BUNDLE_BYTES_EXCEEDED",
        total_source_bytes: totalSourceBytes,
        max_source_bytes: SOURCE_LIMITS.maxBytes,
      });
    }
    const actualSha256 = createHash("sha256").update(file.content, "utf8").digest("hex");
    if (actualSha256 !== file.content_sha256) {
      throw new HttpContractError("INVALID_REQUEST", {
        reason: "SOURCE_CONTENT_HASH_MISMATCH",
        path: file.path,
      });
    }
  }
  if (entrypointMatches !== 1) {
    throw new HttpContractError("INVALID_REQUEST", {
      reason: "SOURCE_ENTRYPOINT_NOT_FOUND",
      entrypoint: bundle.entrypoint,
    });
  }
}

function validateFeishuContextBinding(request, pathname, body, principal) {
  if (!pathname.startsWith("/integrations/feishu/") || body?.context === undefined) return;
  const { requestId, traceId } = requestIdentity(request);
  const mismatchedFields = [];
  if (body.context.request_id !== requestId) mismatchedFields.push("request_id");
  if (body.context.trace_id !== traceId) mismatchedFields.push("trace_id");
  if (body.context.actor.tenant_id !== principal.tenantId) mismatchedFields.push("actor.tenant_id");
  if (body.context.actor.actor_id !== principal.actorId) mismatchedFields.push("actor.actor_id");
  if (body.context.actor.actor_type !== principal.actorType) mismatchedFields.push("actor.actor_type");
  if (body.context.actor.roles.some((role) => !principal.roles.has(role))) mismatchedFields.push("actor.roles");
  if (mismatchedFields.length > 0) {
    throw new HttpContractError("INVALID_REQUEST", {
      reason: "CONTEXT_HEADER_MISMATCH",
      fields: mismatchedFields,
    });
  }
}

function enforceSemantic(check) {
  try {
    check();
  } catch (error) {
    if (error instanceof SemanticInvariantError) {
      throw new HttpContractError(error.code, error.details);
    }
    throw error;
  }
}

function evidenceRefsSignature(refs) {
  return JSON.stringify(refs.map((ref) => [
    ref.evidence_id,
    ref.evidence_type,
    ref.created_at,
    ref.sha256 ?? null,
    ref.uri ?? null,
  ]).sort((left, right) => String(left[0]).localeCompare(String(right[0]), "en")));
}

function validateResponseSemantics(schema, payload) {
  try {
    if (schema === "gameCommand" && payload?.result?.result_type === "WORLD_COMMIT") {
      assertWorldRevisionAdvance(payload.result.previous_revision, payload.result.world_revision, "command result");
      assertEventSequenceRange(
        payload.result.first_event_sequence,
        payload.result.last_event_sequence,
        "command result",
      );
    } else if (schema === "gameRun") {
      assertUniqueEvidenceRefs(payload?.evidence_refs, "run.evidence_refs");
      if (payload?.world_application?.receipt) {
        const receipt = payload.world_application.receipt;
        assertWorldRevisionAdvance(receipt.previous_revision, receipt.world_revision, "run receipt");
        assertEventSequenceRange(
          receipt.first_event_sequence,
          receipt.last_event_sequence,
          "run receipt",
        );
      }
      if (payload?.agent_feedback) {
        assertUniqueEvidenceRefs(payload.agent_feedback.evidence_refs, "run.agent_feedback.evidence_refs");
        assertAgentTurnFeedbackReadyEvent({
          event_type: "agent.turn.feedback_ready",
          stream_id: `agent-session:${payload.agent_feedback.session_id}`,
          command_id: payload.command_id,
          payload: payload.agent_feedback,
        });
        const mismatchedOwnerFields = ["session_id", "turn_id", "command_id", "run_id"]
          .filter((field) => payload.agent_feedback[field] !== payload[field]);
        if (mismatchedOwnerFields.length > 0) {
          throw new SemanticInvariantError(
            "INVARIANT_VIOLATION",
            "run agent feedback owner must equal the owning Run",
            { mismatched_fields: mismatchedOwnerFields },
          );
        }
        if (evidenceRefsSignature(payload.agent_feedback.evidence_refs)
          !== evidenceRefsSignature(payload.evidence_refs)) {
          throw new SemanticInvariantError(
            "INVARIANT_VIOLATION",
            "run and agent feedback must expose the same evidence set",
          );
        }
      }
    } else if (schema === "gameEvidence" && payload?.payload?.evidence_kind === "WORLD_COMMIT") {
      assertWorldCommitEvidence(payload);
    } else if (schema === "gameSkillActivation") {
      assertWorldRevisionAdvance(
        payload.previous_registry_revision,
        payload.registry_revision,
        "skill activation registry",
      );
    } else if (schema === "gameWorldEvents") {
      assertWorldEventPage(payload);
    } else if (schema === "feishuClassResponse") {
      assertClassInsightsPrivacy(payload);
    }
  } catch (error) {
    if (error instanceof SemanticInvariantError) {
      throw new HttpContractError("INTERNAL_ERROR", {
        reason: "RESPONSE_SEMANTIC_INVARIANT",
        invariant_code: error.code,
        schema,
      });
    }
    throw error;
  }
}

function responseHeader(headers, name) {
  const entry = Object.entries(headers ?? {})
    .find(([headerName]) => headerName.toLowerCase() === name.toLowerCase());
  return entry?.[1];
}

function validateOutboundResourceIdentity(canonicalResult, outboundResult, request, pathname) {
  const schema = canonicalResult.schema;
  const canonical = canonicalResult.payload;
  const outbound = outboundResult.payload;
  const mismatchedFields = [];
  const anchor = (field, actual, expected) => {
    if (!isDeepStrictEqual(actual, expected)) mismatchedFields.push(field);
  };

  // responseTransform is a fault-injection hook, not a presentation mapper.
  // Resource payloads carry immutable origin and replay identities; the current
  // HTTP-attempt identity is emitted only in response headers. Therefore every
  // schema-valid outbound payload must still equal the canonical materialized
  // result. isDeepStrictEqual ignores plain-object key insertion order.
  anchor("payload", outbound, canonical);

  if (schema === "acceptedGameJob") {
    const canonicalLocation = `/v1/commands/${canonical?.command_id}`;
    anchor("job_id", outbound?.job_id, canonical?.job_id);
    anchor("command_id", outbound?.command_id, canonical?.command_id);
    anchor("trace_id", outbound?.trace_id, canonical?.trace_id);
    anchor("canonical.trace_id", canonical?.trace_id, requestIdentity(request).traceId);
    anchor("Location", responseHeader(outboundResult.headers, "Location"), canonicalLocation);
  } else if (schema === "gameCommand" && /^\/v1\/commands\/[^/]+$/u.test(pathname)) {
    const pathCommandId = decodedSegment(pathname);
    anchor("canonical.command_id", canonical?.command_id, pathCommandId);
    anchor("command_id", outbound?.command_id, canonical?.command_id);
    anchor("request_context", outbound?.request_context, canonical?.request_context);
  } else if (schema === "gameRun" && /^\/v1\/runs\/[^/]+$/u.test(pathname)) {
    const pathRunId = decodedSegment(pathname);
    anchor("canonical.run_id", canonical?.run_id, pathRunId);
    for (const field of ["run_id", "session_id", "turn_id", "command_id"]) {
      anchor(field, outbound?.[field], canonical?.[field]);
    }
    anchor("request_context", outbound?.request_context, canonical?.request_context);
    anchor("evidence_refs", outbound?.evidence_refs, canonical?.evidence_refs);
    anchor(
      "agent_feedback.identity",
      outbound?.agent_feedback === null
        ? null
        : {
            run_id: outbound?.agent_feedback?.run_id,
            session_id: outbound?.agent_feedback?.session_id,
            turn_id: outbound?.agent_feedback?.turn_id,
            command_id: outbound?.agent_feedback?.command_id,
            evidence_refs: outbound?.agent_feedback?.evidence_refs,
          },
      canonical?.agent_feedback === null
        ? null
        : {
            run_id: canonical?.agent_feedback?.run_id,
            session_id: canonical?.agent_feedback?.session_id,
            turn_id: canonical?.agent_feedback?.turn_id,
            command_id: canonical?.agent_feedback?.command_id,
            evidence_refs: canonical?.agent_feedback?.evidence_refs,
          },
    );
  } else if (schema === "gameEvidence" && /^\/v1\/evidence\/[^/]+$/u.test(pathname)) {
    const pathEvidenceId = decodedSegment(pathname);
    anchor("canonical.evidence_id", canonical?.evidence_ref?.evidence_id, pathEvidenceId);
    anchor("evidence_ref", outbound?.evidence_ref, canonical?.evidence_ref);
    anchor("request_context", outbound?.request_context, canonical?.request_context);
    anchor("subject", outbound?.subject, canonical?.subject);
    anchor("source", outbound?.source, canonical?.source);
  }

  if (mismatchedFields.length > 0) {
    throw new HttpContractError("INTERNAL_ERROR", {
      reason: "RESPONSE_RESOURCE_IDENTITY_MISMATCH",
      schema,
      fields: mismatchedFields,
    });
  }
}

function acceptedJob(kind, request, scope) {
  const suffix = createHash("sha256").update(`${kind}:${scope}`).digest("hex").slice(0, 16);
  return {
    job_id: `job_${suffix}`,
    job_type: kind,
    status: "ACCEPTED",
    created_at: "2026-08-06T10:00:00Z",
    updated_at: "2026-08-06T10:00:00Z",
    command_id: `cmd_${suffix}`,
    trace_id: requestIdentity(request).traceId,
    error: null,
  };
}

function tenantResourceKey(tenantId, resourceId) {
  return `${tenantId}:${resourceId}`;
}

function requireOriginActor(resource, principal, details = {}) {
  const actor = resource?.request_context?.actor;
  const roles = actor?.roles;
  const principalRoles = [...principal.roles].sort();
  if (!actor
    || actor.tenant_id !== principal.tenantId
    || actor.actor_id !== principal.actorId
    || actor.actor_type !== principal.actorType
    || !Array.isArray(roles)
    || roles.length !== principalRoles.length
    || roles.some((role, index) => role !== principalRoles[index])) {
    throw new HttpContractError("NOT_FOUND", { reason: "RESOURCE_ACTOR_MISMATCH", ...details });
  }
  return resource;
}

function requireWorld(state, principal, worldId) {
  if (principal.tenantId !== state.worldTenantId || worldId !== state.worldSnapshot.world_id) {
    throw new HttpContractError("NOT_FOUND", { resource: "world", world_id: worldId });
  }
  return requireOriginActor(state.worldSnapshot, principal, { resource: "world", world_id: worldId });
}

function requireAgentProfile(state, principal, agentProfileId) {
  if (!state.agentProfiles.has(tenantResourceKey(principal.tenantId, agentProfileId))) {
    throw new HttpContractError("NOT_FOUND", {
      resource: "agent_profile",
      agent_profile_id: agentProfileId,
    });
  }
}

function restoreState(state, checkpoint) {
  for (const key of Object.keys(state)) delete state[key];
  Object.assign(state, checkpoint);
}

function materializeWorldCommitEvidence({
  command,
  commandId,
  completedAt,
  principal,
  receipt,
  runId,
  session,
  skillBinding,
}) {
  const payload = {
    evidence_kind: "WORLD_COMMIT",
    world_id: receipt.world_id,
    previous_revision: receipt.previous_revision,
    world_revision: receipt.world_revision,
    first_event_sequence: receipt.first_event_sequence,
    last_event_sequence: receipt.last_event_sequence,
    state_hash: receipt.state_hash,
  };
  const payloadSha256 = canonicalJsonSha256V1(payload);
  const evidenceId = `evidence_${createHash("sha256")
    .update(`${principal.tenantId}:${principal.actorId}:${commandId}:${runId}`)
    .digest("hex")
    .slice(0, 24)}`;
  const evidenceRef = {
    evidence_id: evidenceId,
    evidence_type: "WORLD_COMMIT",
    created_at: completedAt,
    sha256: payloadSha256,
  };
  const evidence = structuredClone(EXAMPLES.evidence);
  evidence.request_context = structuredClone(command.request_context);
  evidence.evidence_ref = structuredClone(evidenceRef);
  evidence.subject.learner_id = session.learner_id;
  evidence.source = {
    source_type: "WORLD",
    source_id: receipt.world_id,
    command_id: commandId,
    world_id: receipt.world_id,
  };
  evidence.occurred_at = completedAt;
  evidence.recorded_at = completedAt;
  evidence.integrity = {
    payload_sha256: payloadSha256,
    previous_evidence_sha256: null,
  };
  evidence.payload = payload;
  evidence.related_evidence = [];
  evidence.versions.skill_version = skillBinding.skill_version_id;
  evidence.versions.artifact_sha256 = skillBinding.artifact_sha256;
  return { evidence, evidenceRef };
}

function commandFor(state, commandId, commandType, request, principal, body = {}, pathname = "", nowMs = Date.now()) {
  const command = contextualResponse(EXAMPLES.command, request, principal);
  command.command_id = commandId;
  command.command_type = commandType;
  command.evidence_refs = [];
  command.links = { self: `/v1/commands/${commandId}` };
  const suffix = createHash("sha256").update(commandId).digest("hex").slice(0, 16);
  const completedAt = new Date(nowMs).toISOString();
  command.request_context.requested_at = completedAt;
  command.accepted_at = completedAt;
  command.updated_at = completedAt;
  if (commandType === "CREATE_SKILL_BUILD") {
    const buildId = `build_${suffix}`;
    const skillVersionId = `skillver_${suffix}`;
    const certificationId = `cert_${suffix}`;
    const sourceSha256 = createHash("sha256")
      .update(JSON.stringify(body.source_bundle.files.map((file) => [file.path, file.content_sha256])))
      .digest("hex");
    const artifactSha256 = createHash("sha256").update(`${commandId}:${sourceSha256}`).digest("hex");
    const build = contextualResponse(EXAMPLES.skillBuild, request, principal, completedAt);
    build.build_id = buildId;
    build.skill_id = body.skill_id;
    build.skill_version_id = skillVersionId;
    build.artifact.source_sha256 = sourceSha256;
    build.artifact.artifact_sha256 = artifactSha256;
    build.artifact.compiler_profile = body.compiler_profile;
    build.artifact.test_suite_version = body.test_suite_version;
    build.certification.certification_id = certificationId;
    build.certification.capabilities = structuredClone(body.requested_capabilities ?? []);
    build.versions.skill_version = skillVersionId;
    build.versions.artifact_sha256 = artifactSha256;
    build.versions.test_suite_version = body.test_suite_version;
    state.skillBuilds.set(tenantResourceKey(principal.tenantId, buildId), build);
    state.skillVersions.set(tenantResourceKey(principal.tenantId, skillVersionId), {
      skill_id: body.skill_id,
      skill_version_id: skillVersionId,
      certification_id: certificationId,
      artifact_sha256: artifactSha256,
    });
    command.result = {
      result_type: "RESOURCE_CREATED",
      resource_type: "SKILL_BUILD",
      resource_id: buildId,
      resource_url: `/v1/skill-builds/${buildId}`,
    };
  } else if (commandType === "ACTIVATE_SKILL_VERSION") {
    const skillVersionId = decodedSegment(pathname, 2);
    const skill = state.skillVersions.get(tenantResourceKey(principal.tenantId, skillVersionId));
    const previousRegistryRevision = state.registryRevisions.get(principal.tenantId) ?? 0;
    const registryRevision = previousRegistryRevision + 1;
    const activationId = `activation_${suffix}`;
    const activation = contextualResponse(EXAMPLES.skillActivation, request, principal, completedAt);
    activation.activation_id = activationId;
    activation.skill_id = skill.skill_id;
    activation.skill_version_id = skill.skill_version_id;
    activation.certification_id = skill.certification_id;
    activation.artifact_sha256 = skill.artifact_sha256;
    activation.activation_scope = structuredClone(body.activation_scope);
    activation.previous_registry_revision = previousRegistryRevision;
    activation.registry_revision = registryRevision;
    activation.activated_at = completedAt;
    state.registryRevisions.set(principal.tenantId, registryRevision);
    state.skillActivations.set(tenantResourceKey(principal.tenantId, activationId), activation);
    command.result = {
      result_type: "RESOURCE_CREATED",
      resource_type: "SKILL_ACTIVATION",
      resource_id: activationId,
      resource_url: `/v1/skill-activations/${activationId}`,
    };
  } else if (commandType === "CREATE_AGENT_SESSION") {
    const sessionId = `session_${suffix}`;
    const session = contextualResponse(EXAMPLES.agentSession, request, principal, completedAt);
    session.session_id = sessionId;
    session.world_id = body.world_id;
    session.learner_id = body.learner_id;
    session.agent_profile_id = body.agent_profile_id;
    session.channel = body.channel;
    session.content = structuredClone(body.content);
    session.request_context.content_ref = structuredClone(body.content);
    session.links = {
      self: `/v1/agent-sessions/${sessionId}`,
      turns: `/v1/agent-sessions/${sessionId}/turns`,
      world_snapshot: `/v1/worlds/${body.world_id}/snapshot`,
    };
    state.agentSessions.set(tenantResourceKey(principal.tenantId, sessionId), session);
    command.result = {
      result_type: "RESOURCE_CREATED",
      resource_type: "AGENT_SESSION",
      resource_id: sessionId,
      resource_url: `/v1/agent-sessions/${sessionId}`,
    };
  } else if (commandType === "EXECUTE_AGENT_TURN") {
    const sessionId = decodedSegment(pathname, 2);
    const session = state.agentSessions.get(tenantResourceKey(principal.tenantId, sessionId));
    if (!session) throw new HttpContractError("NOT_FOUND", { resource: "agent_session", session_id: sessionId });
    requireOriginActor(session, principal, { resource: "agent_session", session_id: sessionId });
    requireWorld(state, principal, session.world_id);
    if (body.expected_world_revision !== state.worldSnapshot.revision) {
      throw new HttpContractError("WORLD_REVISION_CONFLICT", {
        expected_world_revision: body.expected_world_revision,
        current_world_revision: state.worldSnapshot.revision,
      });
    }
    if (body.client_state.last_event_sequence !== state.worldSnapshot.last_event_sequence) {
      throw new HttpContractError("EVENT_SEQUENCE_GAP", {
        supplied_last_event_sequence: body.client_state.last_event_sequence,
        current_last_event_sequence: state.worldSnapshot.last_event_sequence,
        snapshot_url: `/v1/worlds/${session.world_id}/snapshot`,
      });
    }
    if (body.client_state.client_turn_sequence !== session.last_turn_sequence + 1) {
      throw new HttpContractError("EVENT_SEQUENCE_GAP", {
        supplied_client_turn_sequence: body.client_state.client_turn_sequence,
        expected_client_turn_sequence: session.last_turn_sequence + 1,
      });
    }
    for (const binding of body.skill_bindings) {
      const certified = state.skillVersions.get(
        tenantResourceKey(principal.tenantId, binding.skill_version_id),
      );
      if (!certified) {
        throw new HttpContractError("SKILL_NOT_CERTIFIED", {
          skill_id: binding.skill_id,
          skill_version_id: binding.skill_version_id,
        });
      }
      if (certified.skill_id !== binding.skill_id
        || certified.certification_id !== binding.certification_id
        || certified.artifact_sha256 !== binding.artifact_sha256) {
        throw new HttpContractError("SKILL_VERSION_MISMATCH", {
          skill_id: binding.skill_id,
          skill_version_id: binding.skill_version_id,
        });
      }
    }
    const previousRevision = state.worldSnapshot.revision;
    const worldRevision = previousRevision + 1;
    const firstEventSequence = state.worldSnapshot.last_event_sequence + 1;
    const lastEventSequence = firstEventSequence + 1;
    const runId = `run_${suffix}`;
    command.result = {
      result_type: "WORLD_COMMIT",
      world_id: session.world_id,
      previous_revision: previousRevision,
      world_revision: worldRevision,
      first_event_sequence: firstEventSequence,
      last_event_sequence: lastEventSequence,
    };
    enforceSemantic(() => assertWorldRevisionAdvance(previousRevision, worldRevision));
    const run = contextualResponse(EXAMPLES.run, request, principal, completedAt);
    const binding = body.skill_bindings[0];
    run.run_id = runId;
    run.session_id = sessionId;
    run.turn_id = body.turn_id;
    run.command_id = commandId;
    run.request_context = structuredClone(command.request_context);
    run.skill = structuredClone(binding);
    run.sandbox.invocation_id = `invoke_${suffix}`;
    run.sandbox.started_at = completedAt;
    run.sandbox.finished_at = completedAt;
    for (const [intentIndex, intent] of run.sandbox.action_intents.entries()) {
      intent.intent_id = `intent_${createHash("sha256")
        .update(`${commandId}:${intentIndex}`)
        .digest("hex")
        .slice(0, 24)}`;
      intent.expected_world_revision = previousRevision;
    }
    const waterIntent = run.sandbox.action_intents.find((intent) => intent.action_type === "WATER");
    const targetPlot = waterIntent
      ? state.worldSnapshot.state.plots.find((plot) => plot.plot_id === waterIntent.plot_id)
      : undefined;
    if (!waterIntent || !targetPlot) {
      throw new Error("Real Agent turn fixture must produce one WATER intent for an existing plot.");
    }
    const nextHydration = Math.min(10_000, targetPlot.hydration + waterIntent.amount_ml);
    const stateHash = createHash("sha256")
      .update(`${session.world_id}:${worldRevision}:${lastEventSequence}:${nextHydration}`)
      .digest("hex");
    run.world_application.receipt.world_id = session.world_id;
    run.world_application.receipt.previous_revision = previousRevision;
    run.world_application.receipt.world_revision = worldRevision;
    run.world_application.receipt.first_event_sequence = firstEventSequence;
    run.world_application.receipt.last_event_sequence = lastEventSequence;
    run.world_application.receipt.state_hash = stateHash;
    run.world_application.receipt.committed_at = completedAt;
    run.agent_feedback.session_id = sessionId;
    run.agent_feedback.turn_id = body.turn_id;
    run.agent_feedback.command_id = commandId;
    run.agent_feedback.run_id = runId;
    run.agent_feedback.completed_at = completedAt;
    run.created_at = completedAt;
    run.updated_at = completedAt;
    run.versions.skill_version = binding.skill_version_id;
    run.versions.artifact_sha256 = binding.artifact_sha256;
    const { evidence, evidenceRef } = materializeWorldCommitEvidence({
      command,
      commandId,
      completedAt,
      principal,
      receipt: run.world_application.receipt,
      runId,
      session,
      skillBinding: binding,
    });
    run.evidence_refs = [structuredClone(evidenceRef)];
    run.agent_feedback.evidence_refs = [structuredClone(evidenceRef)];
    command.evidence_refs = [structuredClone(evidenceRef)];
    state.evidence.set(tenantResourceKey(principal.tenantId, evidenceRef.evidence_id), evidence);
    state.runs.set(tenantResourceKey(principal.tenantId, runId), run);
    command.links.run = `/v1/runs/${runId}`;
    command.links.world_snapshot = `/v1/worlds/${session.world_id}/snapshot`;
    session.last_turn_sequence = body.client_state.client_turn_sequence;
    session.updated_at = completedAt;
    const eventBase = structuredClone(EXAMPLES.worldEvents.events);
    for (let index = 0; index < eventBase.length; index += 1) {
      const event = eventBase[index];
      event.event_id = `evt_${createHash("sha256").update(`${commandId}:${index}`).digest("hex").slice(0, 24)}`;
      event.stream_id = `world:${session.world_id}`;
      event.sequence = firstEventSequence + index;
      event.occurred_at = completedAt;
      event.trace_id = requestIdentity(request).traceId;
      event.command_id = commandId;
      event.correlation_id = command.request_context.correlation_id;
      event.causation_id = index === 0 ? commandId : eventBase[index - 1].event_id;
      event.content_ref = structuredClone(command.request_context.content_ref);
      event.payload.world_revision = worldRevision;
      state.worldEvents.push(event);
    }
    state.worldSnapshot.revision = worldRevision;
    state.worldSnapshot.last_event_sequence = lastEventSequence;
    state.worldSnapshot.generated_at = completedAt;
    state.worldSnapshot.state_hash = stateHash;
    targetPlot.hydration = nextHydration;
    targetPlot.last_updated_event_sequence = lastEventSequence;
  } else if (commandType === "INGEST_CLIENT_EVENTS") {
    let acceptedCount = 0;
    let duplicateCount = 0;
    for (const event of body.events) {
      const eventKey = tenantResourceKey(principal.tenantId, event.event_id);
      if (state.clientEventIds.has(eventKey)) duplicateCount += 1;
      else {
        state.clientEventIds.add(eventKey);
        acceptedCount += 1;
      }
    }
    command.result = {
      result_type: "CLIENT_EVENTS_ACCEPTED",
      batch_id: body.batch_id,
      accepted_count: acceptedCount,
      duplicate_count: duplicateCount,
      rejected_count: 0,
    };
  } else {
    throw new Error(`Unsupported mock command type ${commandType}`);
  }
  return command;
}

function fixtureId(actual, expected, resource, details = {}) {
  if (actual !== expected) {
    throw new HttpContractError("NOT_FOUND", { resource, requested_id: actual, ...details });
  }
}

function decodedSegment(path, indexFromEnd = 1) {
  return decodeURIComponent(path.split("/").at(-indexFromEnd));
}

function contextualResponse(exampleValue, request, principal, requestedAt = undefined) {
  const value = structuredClone(exampleValue);
  if (value.request_context) {
    const identity = requestIdentity(request);
    value.request_context.request_id = identity.requestId;
    value.request_context.trace_id = identity.traceId;
    value.request_context.correlation_id = identity.correlationId;
    value.request_context.actor.tenant_id = principal.tenantId;
    value.request_context.actor.actor_id = principal.actorId;
    value.request_context.actor.actor_type = principal.actorType;
    value.request_context.actor.roles = [...principal.roles].sort();
    if (typeof requestedAt === "string") value.request_context.requested_at = requestedAt;
  }
  return value;
}

export function signMockFeishuBody(rawBody, timestamp, nonce, secret = DEFAULT_FEISHU_SECRET) {
  const raw = Buffer.isBuffer(rawBody) ? rawBody : Buffer.from(rawBody);
  return createHmac("sha256", secret)
    .update(String(timestamp))
    .update(".")
    .update(String(nonce))
    .update(".")
    .update(raw)
    .digest("hex");
}

function verifyFeishuSignature(request, raw, nowMs, secret) {
  const timestamp = requireHeader(request, "X-Lark-Request-Timestamp");
  const nonce = requireHeader(request, "X-Lark-Request-Nonce");
  const supplied = requireHeader(request, "X-Lark-Signature");
  if (!FEISHU_TIMESTAMP_PATTERN.test(timestamp)) {
    throw new HttpContractError("FEISHU_SIGNATURE_INVALID", { reason: "TIMESTAMP_INVALID_FORMAT" });
  }
  const nonceLength = Array.from(nonce).length;
  if (nonceLength < 1 || nonceLength > 256) {
    throw new HttpContractError("FEISHU_SIGNATURE_INVALID", { reason: "NONCE_INVALID_LENGTH" });
  }
  const timestampNumber = Number(timestamp);
  if (!Number.isInteger(timestampNumber)
    || Math.abs(Math.floor(nowMs / 1000) - timestampNumber) > FEISHU_CLOCK_SKEW_SECONDS) {
    throw new HttpContractError("FEISHU_SIGNATURE_INVALID", { reason: "TIMESTAMP_OUTSIDE_WINDOW" });
  }
  const expected = signMockFeishuBody(raw, timestamp, nonce, secret);
  const suppliedBuffer = Buffer.from(supplied, "utf8");
  const expectedBuffer = Buffer.from(expected, "utf8");
  if (suppliedBuffer.length !== expectedBuffer.length || !timingSafeEqual(suppliedBuffer, expectedBuffer)) {
    throw new HttpContractError("FEISHU_SIGNATURE_INVALID", { reason: "SIGNATURE_MISMATCH" });
  }
  return { timestamp, nonce };
}

function webhookResult(request, body, raw, state, signatureMeta, nowMs) {
  if (body.type === "url_verification") {
    return { status: 200, payload: { challenge: body.challenge }, schema: "feishuWebhookResponse" };
  }
  const eventId = body.header.event_id;
  const tenantKey = body.header.tenant_key;
  const eventKey = `${tenantKey}:${eventId}`;
  const bodyHash = createHash("sha256").update(raw).digest("hex");
  const existing = state.webhookEvents.get(eventKey);
  if (existing) {
    if (existing.bodyHash !== bodyHash) {
      throw new HttpContractError("IDEMPOTENCY_KEY_REUSED", { event_id: eventId });
    }
    return {
      status: 200,
      payload: { ...existing.payload, disposition: "DUPLICATE", received_at: new Date(nowMs).toISOString() },
      schema: "feishuWebhookResponse",
    };
  }

  const nonceKey = `${tenantKey}:${signatureMeta.timestamp}:${signatureMeta.nonce}`;
  const usedNonce = state.webhookNonces.get(nonceKey);
  if (usedNonce && usedNonce !== bodyHash) {
    throw new HttpContractError("FEISHU_REPLAY_DETECTED", { tenant_key: tenantKey });
  }
  state.webhookNonces.set(nonceKey, bodyHash);

  const supported = SUPPORTED_FEISHU_EVENTS.has(body.header.event_type);
  const suffix = createHash("sha256").update(eventKey).digest("hex").slice(0, 16);
  const payload = {
    event_id: eventId,
    receipt_id: `fwr_${suffix}`,
    disposition: supported ? "ACCEPTED" : "QUARANTINED_UNSUPPORTED",
    received_at: new Date(nowMs).toISOString(),
    trace_id: requestIdentity(request).traceId,
    ...(supported ? {} : { quarantine_reason: `unsupported_event_type:${body.header.event_type}` }),
  };
  state.webhookEvents.set(eventKey, { bodyHash, payload });
  return { status: 200, payload, schema: "feishuWebhookResponse" };
}

function learnerProjectionFor(body) {
  const result = structuredClone(EXAMPLES.feishuLearner);
  const suffix = createHash("sha256")
    .update(`${body.context.request_id}:${body.learner_ref}`)
    .digest("hex")
    .slice(0, 16);
  result.query_id = `lqry_${suffix}`;
  result.learner_ref = body.learner_ref;
  result.trace_id = body.context.trace_id;
  const optionalFields = new Map([
    ["MASTERY_SUMMARY", "mastery_summary"],
    ["RECENT_EVIDENCE", "recent_evidence"],
    ["SUPPORT_NEEDS", "support_needs"],
  ]);
  for (const [requestedField, responseField] of optionalFields) {
    if (!body.requested_fields.includes(requestedField)) delete result[responseField];
  }
  return result;
}

function classInsightsFor(body) {
  const result = structuredClone(EXAMPLES.feishuClass);
  const suffix = createHash("sha256")
    .update(`${body.context.request_id}:${body.class_ref}`)
    .digest("hex")
    .slice(0, 16);
  result.query_id = `ciq_${suffix}`;
  result.class_ref = body.class_ref;
  result.trace_id = body.context.trace_id;
  result.privacy.minimum_cohort_size = body.privacy.minimum_cohort_size;
  result.privacy.effective_minimum_cohort_size = Math.max(5, body.privacy.minimum_cohort_size);
  const requestedDimensions = new Set(body.dimensions);
  result.insights = result.insights.filter((insight) => requestedDimensions.has(insight.dimension));
  const effective = result.privacy.effective_minimum_cohort_size;
  for (const insight of result.insights) {
    if (result.cohort_size < effective || (insight.learner_count !== null && insight.learner_count < effective)) {
      insight.learner_count = null;
      insight.ratio = null;
      insight.suppressed = true;
    }
  }
  return result;
}

function contentReleaseFor(body, request, state, idempotencyScope, nowMs) {
  const suffix = createHash("sha256")
    .update(`content-release:${idempotencyScope}`)
    .digest("hex")
    .slice(0, 16);
  const createdAt = new Date(nowMs).toISOString();
  const traceId = requestIdentity(request).traceId;
  const releaseId = `rel_${suffix}`;
  const candidateId = `cand_${suffix}`;
  const jobId = `job_${suffix}`;
  const commandId = `cmd_${suffix}`;

  const receipt = structuredClone(EXAMPLES.feishuRelease);
  receipt.release_id = releaseId;
  receipt.candidate_id = candidateId;
  receipt.candidate_revision = 1;
  receipt.trace_id = traceId;
  receipt.created_at = createdAt;
  receipt.validation_job.job_id = jobId;
  receipt.validation_job.command_id = commandId;
  receipt.validation_job.trace_id = traceId;
  receipt.validation_job.created_at = createdAt;
  receipt.validation_job.updated_at = createdAt;

  const status = structuredClone(EXAMPLES.feishuReleaseStatus);
  status.release_id = releaseId;
  status.candidate_id = candidateId;
  status.candidate_revision = 1;
  status.trace_id = traceId;
  status.created_at = createdAt;
  status.updated_at = createdAt;
  status.validation_job.job_id = jobId;
  status.validation_job.command_id = commandId;
  status.validation_job.trace_id = traceId;
  status.validation_job.created_at = createdAt;
  status.validation_job.updated_at = createdAt;
  status.lifecycle_status = "CANDIDATE_CREATED";
  status.validation_job.status = "QUEUED";
  for (const check of status.checks) {
    check.status = "PENDING";
    delete check.evidence_refs;
    delete check.error;
  }
  state.contentReleases.set(tenantResourceKey(body.context.actor.tenant_id, releaseId), status);
  return receipt;
}

function approvalDecisionFor(body, request, idempotencyScope, nowMs, committedRevision) {
  const suffix = createHash("sha256")
    .update(`approval-decision:${idempotencyScope}`)
    .digest("hex")
    .slice(0, 16);
  const receipt = structuredClone(EXAMPLES.feishuApproval);
  receipt.decision_id = `dec_${suffix}`;
  receipt.release_id = body.release_id;
  receipt.candidate_id = body.candidate_id;
  receipt.candidate_revision = committedRevision;
  receipt.next_step = body.decision === "APPROVE"
    ? "VALIDATION_REQUIRED"
    : body.decision === "REQUEST_CHANGES"
      ? "WAITING_FOR_CHANGES"
      : "WORKFLOW_CLOSED";
  receipt.trace_id = requestIdentity(request).traceId;
  receipt.recorded_at = new Date(nowMs).toISOString();
  return receipt;
}

function canonicalApprovalDecision(body) {
  return {
    release_id: body.release_id,
    candidate_id: body.candidate_id,
    expected_candidate_revision: body.expected_candidate_revision,
    decision: body.decision,
    comment: body.comment ?? null,
    decided_at: body.decided_at,
    actor: {
      tenant_id: body.context.actor.tenant_id,
      actor_id: body.context.actor.actor_id,
      actor_type: body.context.actor.actor_type,
      roles: [...body.context.actor.roles].sort(),
    },
    content_ref: {
      unit_id: body.context.content_ref.unit_id,
      version: body.context.content_ref.version,
      content_hash: body.context.content_ref.content_hash,
    },
  };
}

function conflictingApprovalFields(recorded, attempted) {
  return Object.keys(recorded).filter((field) => (
    JSON.stringify(recorded[field]) !== JSON.stringify(attempted[field])
  ));
}

function releaseCandidateStateKey(tenantId, releaseId, candidateId) {
  return tenantResourceKey(tenantId, `${releaseId}:${candidateId}`);
}

function reportJobFor(body, request, state, idempotencyScope, nowMs) {
  const suffix = createHash("sha256")
    .update(`report-job:${idempotencyScope}`)
    .digest("hex")
    .slice(0, 16);
  const createdAt = new Date(nowMs).toISOString();
  const result = structuredClone(EXAMPLES.feishuReport);
  result.report_id = `rpt_${suffix}`;
  result.job.job_id = `job_${suffix}`;
  result.job.command_id = `cmd_${suffix}`;
  result.job.trace_id = requestIdentity(request).traceId;
  result.job.created_at = createdAt;
  result.job.updated_at = createdAt;
  result.output_mode = body.output_mode;
  state.reportJobs.set(tenantResourceKey(body.context.actor.tenant_id, result.job.job_id), result);
  return result;
}

function resolveRoute(request, url, body, raw, state, context) {
  const { pathname: path, searchParams } = url;
  const method = request.method ?? "GET";
  if (method === "GET" && path === "/health") {
    return { status: 200, payload: { status: "ok", mode: "contract_mock", contract_version: "0.1.0" } };
  }

  if (method === "POST" && path === "/integrations/feishu/v1/webhooks") {
    return webhookResult(request, body, raw, state, context.signatureMeta, context.nowMs);
  }
  if (method === "GET" && path === "/v1/bootstrap") {
    requireWorld(state, context.principal, state.worldSnapshot.world_id);
    const bootstrap = contextualResponse(
      EXAMPLES.bootstrap,
      request,
      context.principal,
      new Date(context.nowMs).toISOString(),
    );
    bootstrap.world.revision = state.worldSnapshot.revision;
    bootstrap.world.last_event_sequence = state.worldSnapshot.last_event_sequence;
    return { status: 200, payload: bootstrap, schema: "gameBootstrap" };
  }
  if (method === "POST" && path === "/v1/skill-builds") return context.accepted("CREATE_SKILL_BUILD", body);
  if (method === "GET" && /^\/v1\/skill-builds\/[^/]+$/u.test(path)) {
    const buildId = decodedSegment(path);
    const build = state.skillBuilds.get(tenantResourceKey(context.principal.tenantId, buildId));
    if (!build) throw new HttpContractError("NOT_FOUND", { resource: "skill_build", build_id: buildId });
    requireOriginActor(build, context.principal, { resource: "skill_build", build_id: buildId });
    return { status: 200, payload: structuredClone(build), schema: "gameSkillBuild" };
  }
  if (method === "POST" && /^\/v1\/skill-versions\/[^/]+\/activations$/u.test(path)) {
    const skillVersionId = decodedSegment(path, 2);
    if (!state.skillVersions.has(tenantResourceKey(context.principal.tenantId, skillVersionId))) {
      throw new HttpContractError("NOT_FOUND", { resource: "skill_version", skill_version_id: skillVersionId });
    }
    requireWorld(state, context.principal, body.activation_scope.world_id);
    requireAgentProfile(state, context.principal, body.activation_scope.agent_profile_id);
    const registryRevision = state.registryRevisions.get(context.principal.tenantId) ?? 0;
    if (body.expected_registry_revision !== registryRevision) {
      throw new HttpContractError("SKILL_VERSION_MISMATCH", {
        expected_registry_revision: body.expected_registry_revision,
        current_registry_revision: registryRevision,
      });
    }
    return context.accepted("ACTIVATE_SKILL_VERSION", body);
  }
  if (method === "GET" && /^\/v1\/skill-activations\/[^/]+$/u.test(path)) {
    const activationId = decodedSegment(path);
    const activation = state.skillActivations.get(
      tenantResourceKey(context.principal.tenantId, activationId),
    );
    if (!activation) {
      throw new HttpContractError("NOT_FOUND", { resource: "skill_activation", activation_id: activationId });
    }
    requireOriginActor(activation, context.principal, {
      resource: "skill_activation",
      activation_id: activationId,
    });
    return {
      status: 200,
      payload: structuredClone(activation),
      schema: "gameSkillActivation",
    };
  }
  if (method === "POST" && path === "/v1/agent-sessions") {
    requireWorld(state, context.principal, body.world_id);
    requireAgentProfile(state, context.principal, body.agent_profile_id);
    if (body.expected_world_revision !== undefined && body.expected_world_revision !== state.worldSnapshot.revision) {
      throw new HttpContractError("WORLD_REVISION_CONFLICT", {
        expected_world_revision: body.expected_world_revision,
        current_world_revision: state.worldSnapshot.revision,
      });
    }
    return context.accepted("CREATE_AGENT_SESSION", body);
  }
  if (method === "GET" && /^\/v1\/agent-sessions\/[^/]+$/u.test(path)) {
    const sessionId = decodedSegment(path);
    const session = state.agentSessions.get(tenantResourceKey(context.principal.tenantId, sessionId));
    if (!session) throw new HttpContractError("NOT_FOUND", { resource: "agent_session", session_id: sessionId });
    requireOriginActor(session, context.principal, { resource: "agent_session", session_id: sessionId });
    return { status: 200, payload: structuredClone(session), schema: "gameAgentSession" };
  }
  if (method === "POST" && /^\/v1\/agent-sessions\/[^/]+\/turns$/u.test(path)) return context.accepted("EXECUTE_AGENT_TURN", body);
  if (method === "GET" && /^\/v1\/commands\/[^/]+$/u.test(path)) {
    const commandId = decodeURIComponent(path.split("/").at(-1));
    const command = state.commands.get(tenantResourceKey(context.principal.tenantId, commandId));
    if (!command) throw new HttpContractError("NOT_FOUND", { resource: "command", command_id: commandId });
    requireOriginActor(command, context.principal, { resource: "command", command_id: commandId });
    return { status: 200, payload: structuredClone(command), schema: "gameCommand" };
  }
  if (method === "GET" && /^\/v1\/runs\/[^/]+$/u.test(path)) {
    const runId = decodedSegment(path);
    const run = state.runs.get(tenantResourceKey(context.principal.tenantId, runId));
    if (!run) throw new HttpContractError("NOT_FOUND", { resource: "run", run_id: runId });
    requireOriginActor(run, context.principal, { resource: "run", run_id: runId });
    return { status: 200, payload: structuredClone(run), schema: "gameRun" };
  }
  if (method === "GET" && /^\/v1\/worlds\/[^/]+\/snapshot$/u.test(path)) {
    const worldId = decodedSegment(path, 2);
    requireWorld(state, context.principal, worldId);
    const snapshot = structuredClone(state.worldSnapshot);
    return {
      status: 200,
      payload: snapshot,
      schema: "gameSnapshot",
      headers: { ETag: `"${snapshot.state_hash}"`, "X-World-Revision": String(snapshot.revision) },
    };
  }
  if (method === "GET" && /^\/v1\/worlds\/[^/]+\/events$/u.test(path)) {
    const worldId = decodedSegment(path, 2);
    requireWorld(state, context.principal, worldId);
    const afterRaw = searchParams.get("after_sequence");
    if (!afterRaw || !/^[0-9]+$/u.test(afterRaw)) {
      throw new HttpContractError("INVALID_REQUEST", { missing_or_invalid_query: "after_sequence" });
    }
    const after = Number(afterRaw);
    const limitRaw = searchParams.get("limit") ?? "100";
    if (!/^[0-9]+$/u.test(limitRaw) || Number(limitRaw) < 1 || Number(limitRaw) > 500) {
      throw new HttpContractError("INVALID_REQUEST", { missing_or_invalid_query: "limit" });
    }
    const limit = Number(limitRaw);
    const earliestAfter = EXAMPLES.bootstrap.world.last_event_sequence;
    const latestAfter = state.worldSnapshot.last_event_sequence;
    if (after < earliestAfter || after > latestAfter) {
      throw new HttpContractError("EVENT_SEQUENCE_GAP", {
        requested_after_sequence: after,
        available_after_sequence: earliestAfter,
        snapshot_url: "/v1/worlds/world_demo_001/snapshot",
      });
    }
    const page = contextualResponse(
      EXAMPLES.worldEvents,
      request,
      context.principal,
      new Date(context.nowMs).toISOString(),
    );
    page.snapshot_revision = state.worldSnapshot.revision;
    const remainingEvents = state.worldEvents.filter((event) => event.sequence > after);
    page.events = remainingEvents.slice(0, limit);
    if (page.events.length === 0) {
      page.from_sequence = after;
      page.to_sequence = after;
      page.next_after_sequence = after;
      page.has_more = remainingEvents.length > page.events.length;
    } else {
      page.from_sequence = page.events[0].sequence;
      page.to_sequence = page.events.at(-1).sequence;
      page.next_after_sequence = page.to_sequence;
      page.has_more = remainingEvents.length > page.events.length;
    }
    enforceSemantic(() => assertWorldEventPage(page, { expectedAfterSequence: after }));
    return {
      status: 200,
      payload: page,
      schema: "gameWorldEvents",
      headers: { "X-World-Revision": String(page.snapshot_revision) },
    };
  }
  if (method === "POST" && path === "/v1/client-events:batch") {
    requireWorld(state, context.principal, body.world_id);
    const session = state.agentSessions.get(
      tenantResourceKey(context.principal.tenantId, body.session_id),
    );
    if (!session || session.world_id !== body.world_id) {
      throw new HttpContractError("NOT_FOUND", { resource: "agent_session", session_id: body.session_id });
    }
    requireOriginActor(session, context.principal, { resource: "agent_session", session_id: body.session_id });
    return context.accepted("INGEST_CLIENT_EVENTS", body);
  }
  if (method === "GET" && /^\/v1\/evidence\/[^/]+$/u.test(path)) {
    const evidenceId = decodedSegment(path);
    const storedEvidence = state.evidence.get(
      tenantResourceKey(context.principal.tenantId, evidenceId),
    );
    if (!storedEvidence) {
      throw new HttpContractError("NOT_FOUND", { resource: "evidence", evidence_id: evidenceId });
    }
    requireOriginActor(storedEvidence, context.principal, {
      resource: "evidence",
      evidence_id: evidenceId,
    });
    const evidence = structuredClone(storedEvidence);
    if (evidence.payload.evidence_kind === "WORLD_COMMIT") {
      enforceSemantic(() => assertWorldRevisionAdvance(
        evidence.payload.previous_revision,
        evidence.payload.world_revision,
        "WORLD_COMMIT evidence",
      ));
    }
    return {
      status: 200,
      payload: evidence,
      schema: "gameEvidence",
      headers: { ETag: `"${evidence.evidence_ref.sha256}"` },
    };
  }

  if (method === "POST" && path === "/integrations/feishu/v1/content-releases") {
    const receipt = contentReleaseFor(body, request, state, context.idempotencyScope, context.nowMs);
    return {
      status: 202,
      payload: receipt,
      schema: "feishuReleaseResponse",
      headers: {
        Location: `/integrations/feishu/v1/content-releases/${receipt.release_id}`,
        "Retry-After": "1",
        "Idempotency-Replayed": "false",
      },
    };
  }
  if (method === "GET" && /^\/integrations\/feishu\/v1\/content-releases\/[^/]+$/u.test(path)) {
    const releaseId = decodedSegment(path);
    const status = state.contentReleases.get(tenantResourceKey(context.principal.tenantId, releaseId));
    if (!status) throw new HttpContractError("NOT_FOUND", { resource: "content_release", release_id: releaseId });
    return { status: 200, payload: status, schema: "feishuReleaseStatus" };
  }
  if (method === "POST" && path === "/integrations/feishu/v1/approval-decisions") {
    const canonicalDecision = canonicalApprovalDecision(body);
    const approvalInstanceKey = tenantResourceKey(
      context.principal.tenantId,
      body.approval_instance_id,
    );
    const recordedInstance = state.approvalInstances.get(approvalInstanceKey);
    if (recordedInstance) {
      const conflictingFields = conflictingApprovalFields(
        recordedInstance.decision,
        canonicalDecision,
      );
      if (conflictingFields.length > 0) {
        throw new HttpContractError("CONTENT_VERSION_MISMATCH", {
          reason: "APPROVAL_INSTANCE_IMMUTABLE",
          approval_instance_id: body.approval_instance_id,
          conflicting_fields: conflictingFields,
        });
      }
      return {
        status: 200,
        payload: structuredClone(recordedInstance.receipt),
        schema: "feishuApprovalResponse",
      };
    }
    const release = state.contentReleases.get(
      tenantResourceKey(context.principal.tenantId, body.release_id),
    );
    if (!release || release.candidate_id !== body.candidate_id) {
      throw new HttpContractError("NOT_FOUND", {
        resource: "content_release_candidate",
        release_id: body.release_id,
        candidate_id: body.candidate_id,
      });
    }
    const candidateStateKey = releaseCandidateStateKey(
      context.principal.tenantId,
      body.release_id,
      body.candidate_id,
    );
    if (state.closedReleaseCandidates.has(candidateStateKey)) {
      throw new HttpContractError("CONTENT_VERSION_MISMATCH", {
        reason: "CANDIDATE_WORKFLOW_CLOSED",
        release_id: body.release_id,
        candidate_id: body.candidate_id,
        expected_candidate_revision: body.expected_candidate_revision,
        current_candidate_revision: release.candidate_revision,
      });
    }
    if (body.expected_candidate_revision !== release.candidate_revision) {
      throw new HttpContractError("CONTENT_VERSION_MISMATCH", {
        resource: "content_release_candidate",
        release_id: body.release_id,
        candidate_id: body.candidate_id,
        expected_candidate_revision: body.expected_candidate_revision,
        current_candidate_revision: release.candidate_revision,
      });
    }
    const committedRevision = release.candidate_revision + 1;
    release.candidate_revision = committedRevision;
    release.updated_at = new Date(context.nowMs).toISOString();
    const receipt = approvalDecisionFor(
      body,
      request,
      context.idempotencyScope,
      context.nowMs,
      committedRevision,
    );
    state.approvalInstances.set(approvalInstanceKey, {
      decision: canonicalDecision,
      receipt: structuredClone(receipt),
    });
    if (receipt.next_step === "WORKFLOW_CLOSED") {
      state.closedReleaseCandidates.add(candidateStateKey);
    }
    return {
      status: 200,
      payload: receipt,
      schema: "feishuApprovalResponse",
    };
  }
  if (method === "POST" && path === "/integrations/feishu/v1/learner-queries") {
    return { status: 200, payload: learnerProjectionFor(body), schema: "feishuLearnerResponse" };
  }
  if (method === "POST" && path === "/integrations/feishu/v1/class-insights") {
    const result = classInsightsFor(body);
    enforceSemantic(() => assertClassInsightsPrivacy(result));
    return { status: 200, payload: result, schema: "feishuClassResponse" };
  }
  if (method === "POST" && path === "/integrations/feishu/v1/report-jobs") {
    const report = reportJobFor(body, request, state, context.idempotencyScope, context.nowMs);
    return {
      status: 202,
      payload: report,
      schema: "feishuReportResponse",
      headers: {
        Location: `/integrations/feishu/v1/report-jobs/${report.job.job_id}`,
        "Retry-After": "1",
        "Idempotency-Replayed": "false",
      },
    };
  }
  if (method === "GET" && /^\/integrations\/feishu\/v1\/report-jobs\/[^/]+$/u.test(path)) {
    const jobId = decodedSegment(path);
    const report = state.reportJobs.get(tenantResourceKey(context.principal.tenantId, jobId));
    if (!report) throw new HttpContractError("NOT_FOUND", { resource: "report_job", job_id: jobId });
    return { status: 200, payload: report, schema: "feishuReportResponse" };
  }
  if (method === "GET" && /^\/integrations\/feishu\/v1\/evidence\/[^/]+$/u.test(path)) {
    if (context.principal.tenantId !== state.worldTenantId) {
      throw new HttpContractError("NOT_FOUND", { resource: "evidence", evidence_id: decodedSegment(path) });
    }
    fixtureId(decodedSegment(path), EXAMPLES.feishuEvidence.evidence_ref.evidence_id, "evidence");
    const purposes = searchParams.getAll("purpose");
    if (purposes.length !== 1 || !FEISHU_EVIDENCE_PURPOSES.has(purposes[0])) {
      throw new HttpContractError("INVALID_REQUEST", { missing_or_invalid_query: "purpose" });
    }
    return { status: 200, payload: EXAMPLES.feishuEvidence, schema: "feishuEvidence" };
  }

  throw new HttpContractError("NOT_FOUND", { path, method });
}

export function createMockServer(options = {}) {
  const initialSnapshot = structuredClone(EXAMPLES.snapshot);
  initialSnapshot.revision = EXAMPLES.bootstrap.world.revision;
  initialSnapshot.last_event_sequence = EXAMPLES.bootstrap.world.last_event_sequence;
  initialSnapshot.state.plots[0].last_updated_event_sequence = initialSnapshot.last_event_sequence;
  initialSnapshot.state_hash = createHash("sha256")
    .update(`${initialSnapshot.world_id}:${initialSnapshot.revision}:${initialSnapshot.last_event_sequence}`)
    .digest("hex");
  const state = {
    commands: new Map(),
    runs: new Map([[
      tenantResourceKey(FIXTURE_TENANT_ID, EXAMPLES.run.run_id),
      structuredClone(EXAMPLES.run),
    ]]),
    evidence: new Map([[
      tenantResourceKey(FIXTURE_TENANT_ID, EXAMPLES.evidence.evidence_ref.evidence_id),
      structuredClone(EXAMPLES.evidence),
    ]]),
    skillBuilds: new Map([[
      tenantResourceKey(FIXTURE_TENANT_ID, EXAMPLES.skillBuild.build_id),
      structuredClone(EXAMPLES.skillBuild),
    ]]),
    skillVersions: new Map([[
      tenantResourceKey(FIXTURE_TENANT_ID, EXAMPLES.skillBuild.skill_version_id),
      {
        skill_id: EXAMPLES.skillBuild.skill_id,
        skill_version_id: EXAMPLES.skillBuild.skill_version_id,
        certification_id: EXAMPLES.skillBuild.certification.certification_id,
        artifact_sha256: EXAMPLES.skillBuild.artifact.artifact_sha256,
      },
    ]]),
    skillActivations: new Map(),
    registryRevisions: new Map([[FIXTURE_TENANT_ID, 17]]),
    agentProfiles: new Set([
      tenantResourceKey(FIXTURE_TENANT_ID, "agent_farmer_001"),
      tenantResourceKey(FIXTURE_TENANT_ID, "agent_farmer_002"),
    ]),
    agentSessions: new Map([[
      tenantResourceKey(FIXTURE_TENANT_ID, EXAMPLES.agentSession.session_id),
      structuredClone(EXAMPLES.agentSession),
    ]]),
    contentReleases: new Map([[
      tenantResourceKey(FIXTURE_TENANT_ID, EXAMPLES.feishuReleaseStatus.release_id),
      structuredClone(EXAMPLES.feishuReleaseStatus),
    ]]),
    approvalInstances: new Map(),
    closedReleaseCandidates: new Set(),
    reportJobs: new Map([[
      tenantResourceKey(FIXTURE_TENANT_ID, EXAMPLES.feishuReport.job.job_id),
      structuredClone(EXAMPLES.feishuReport),
    ]]),
    worldTenantId: FIXTURE_TENANT_ID,
    worldSnapshot: initialSnapshot,
    worldEvents: [],
    clientEventIds: new Set(),
    idempotency: new Map(),
    webhookEvents: new Map(),
    webhookNonces: new Map(),
    auditRecords: [],
  };
  const now = options.now ?? (() => Date.now());
  const feishuSecret = options.feishuSecret ?? DEFAULT_FEISHU_SECRET;
  const responseTransform = options.responseTransform ?? ((payload) => payload);
  const idempotencyCapacity = options.idempotencyCapacity ?? MAX_IDEMPOTENCY_RECORDS;
  if (!Number.isSafeInteger(idempotencyCapacity) || idempotencyCapacity < 1) {
    throw new TypeError("idempotencyCapacity must be a positive safe integer");
  }
  const logInternalError = typeof options.logger === "function"
    ? options.logger
    : options.logger?.error?.bind(options.logger) ?? console.error.bind(console);

  const server = http.createServer(async (request, response) => {
    let method = request.method ?? "GET";
    let url;
    let auditAccess;
    let principal;
    let body;
    try {
      url = new URL(request.url ?? "/", `http://${request.headers.host ?? "127.0.0.1"}`);
      auditAccess = auditedAccessOperation(method, url.pathname);
      validateCommonHeaders(request, url.pathname, method);
      const isWebhook = method === "POST" && url.pathname === "/integrations/feishu/v1/webhooks";
      const isHealth = method === "GET" && url.pathname === "/health";
      principal = isWebhook || isHealth ? { tenantId: "feishu", actorId: "webhook" } : mockPrincipal(request);
      if (!isWebhook && !isHealth) enforceFeishuRole(principal, method, url.pathname);
      const raw = await readRawBody(request);
      const signatureMeta = isWebhook ? verifyFeishuSignature(request, raw, now(), feishuSecret) : undefined;
      body = parseJsonBody(raw);
      const requestContract = requestSchema(method, url.pathname);
      if (requestContract) {
        validateValue(body, requestContract, `${method} ${url.pathname} request`);
        validateRequestSemantics(requestContract, body);
      }
      const idempotencyKey = IDEMPOTENT_METHODS.has(method) ? requireHeader(request, "Idempotency-Key") : "";
      const idempotencyTenant = isWebhook ? body?.header?.tenant_key ?? principal.tenantId : principal.tenantId;
      // A command/resource is actor-bound, so its replay receipt must be actor-bound too.
      // Tenant-only scoping would let a second student discover another student's
      // command id while still being unable to reconcile that command afterwards.
      const scope = `${idempotencyTenant}:${principal.actorId}:${operationScope(method, url.pathname)}:${idempotencyKey}`;
      const requestHash = createHash("sha256").update(`${method}:${url.pathname}:`).update(raw).digest("hex");
      if (IDEMPOTENT_METHODS.has(method)) {
        const previous = state.idempotency.get(scope);
        if (previous && previous.requestHash !== requestHash) {
          throw new HttpContractError("IDEMPOTENCY_KEY_REUSED", { scope: `${method}:${url.pathname}` });
        }
        if (previous) {
          appendAuditRecord(
            state, auditAccess, request, url, body, principal, "ALLOWED", null, previous.result.status, now(),
          );
          const replayHeaders = { ...(previous.result.headers ?? {}) };
          if (previous.result.status === 202) replayHeaders["Idempotency-Replayed"] = "true";
          sendJson(request, response, previous.result.status, previous.result.payload, replayHeaders);
          return;
        }
      }

      // A byte-equivalent idempotent replay retains the origin context in its
      // body while request/trace headers identify the new HTTP attempt. New
      // work must still bind body context to the authenticated first attempt.
      validateFeishuContextBinding(request, url.pathname, body, principal);
      if (IDEMPOTENT_METHODS.has(method) && state.idempotency.size >= idempotencyCapacity) {
        throw new HttpContractError("RATE_LIMITED", {
          stage: "IDEMPOTENCY",
          reason: "IDEMPOTENCY_CAPACITY_EXHAUSTED",
          capacity: idempotencyCapacity,
        });
      }

      const context = {
        nowMs: now(),
        signatureMeta,
        principal,
        idempotencyScope: scope,
        accepted: (kind, commandBody = {}) => {
          const payload = acceptedJob(kind, request, scope);
          const command = commandFor(
            state,
            payload.command_id,
            kind,
            request,
            principal,
            commandBody,
            url.pathname,
            now(),
          );
          state.commands.set(tenantResourceKey(principal.tenantId, payload.command_id), command);
          return {
            status: 202,
            payload,
            schema: "acceptedGameJob",
            headers: {
              Location: `/v1/commands/${payload.command_id}`,
              "Retry-After": "1",
              "Idempotency-Replayed": "false",
            },
          };
        },
      };
      const checkpoint = structuredClone(state);
      let committed = false;
      let canonicalResult;
      try {
        canonicalResult = resolveRoute(request, url, body, raw, state, context);
        if (canonicalResult.schema) {
          validateValue(
            canonicalResult.payload,
            canonicalResult.schema,
            `${method} ${url.pathname} canonical response`,
            "INTERNAL_ERROR",
          );
          validateResponseSemantics(canonicalResult.schema, canonicalResult.payload);
        }
        if (IDEMPOTENT_METHODS.has(method)) {
          state.idempotency.set(scope, {
            requestHash,
            result: structuredClone(canonicalResult),
          });
        }
        committed = true;

        const outboundResult = structuredClone(canonicalResult);
        outboundResult.payload = responseTransform(outboundResult.payload, {
          method,
          pathname: url.pathname,
          status: outboundResult.status,
          schema: outboundResult.schema,
        });
        if (outboundResult.schema) {
          validateValue(
            outboundResult.payload,
            outboundResult.schema,
            `${method} ${url.pathname} response`,
            "INTERNAL_ERROR",
          );
          validateResponseSemantics(outboundResult.schema, outboundResult.payload);
          validateOutboundResourceIdentity(
            canonicalResult,
            outboundResult,
            request,
            url.pathname,
          );
        }
        appendAuditRecord(
          state, auditAccess, request, url, body, principal, "ALLOWED", null, outboundResult.status, now(),
        );
        sendJson(request, response, outboundResult.status, outboundResult.payload, outboundResult.headers);
      } catch (error) {
        if (!committed) restoreState(state, checkpoint);
        if (committed && canonicalResult?.status === 202 && canonicalResult?.payload?.command_id) {
          const reconciliationUrl = `/v1/commands/${canonicalResult.payload.command_id}`;
          throw new HttpContractError(
            "UNKNOWN_COMMIT_STATE",
            {
              reason: "RESPONSE_DELIVERY_FAILED_AFTER_DURABLE_ACCEPT",
              stage: "WORLD_COMMIT",
              command_id: canonicalResult.payload.command_id,
              reconciliation_url: reconciliationUrl,
              operation_was_durably_accepted: true,
            },
            undefined,
            { Location: reconciliationUrl },
          );
        }
        throw error;
      }
    } catch (error) {
      if (error instanceof HttpContractError) {
        const result = errorResponse(request, error.code, error.details, error.statusOverride);
        Object.assign(result.headers, error.headers);
        appendAuditRecord(
          state,
          auditAccess,
          request,
          url,
          body,
          principal,
          auditOutcomeForError(result.payload.error.code),
          result.payload.error.code,
          result.status,
          now(),
        );
        sendJson(request, response, result.status, result.payload, result.headers);
        return;
      }
      const incidentId = `incident_${now()}`;
      const { requestId, traceId } = requestIdentity(request);
      const stack = error instanceof Error ? error.stack ?? error.message : String(error);
      try {
        logInternalError({
          event: "mock_server_internal_error",
          incident_id: incidentId,
          request_id: requestId,
          trace_id: traceId,
          stack,
        });
      } catch {
        // Logging must never replace the sanitized contract response.
      }
      const result = errorResponse(request, "INTERNAL_ERROR", { incident_id: incidentId }, 500);
      appendAuditRecord(
        state, auditAccess, request, url, body, principal, "FAILED", "INTERNAL_ERROR", result.status, now(),
      );
      sendJson(request, response, result.status, result.payload, result.headers);
    }
  });
  Object.defineProperty(server, "getAuditRecords", {
    value: () => structuredClone(state.auditRecords),
  });
  return server;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const server = createMockServer();
  server.listen(DEFAULT_PORT, "127.0.0.1", () => {
    console.log(`YAYA_AGENT_MOCK_READY http://127.0.0.1:${DEFAULT_PORT}`);
  });
}
