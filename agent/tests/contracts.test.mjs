import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import {
  assertSchema,
  loadDocuments,
  PROJECT_ROOT,
  validateContracts,
} from "../scripts/validate-contracts.mjs";
import { MOCK_FEISHU_ROLE_POLICIES } from "../scripts/mock-server.mjs";

const PYTHON_EXE = process.env.YAYA_PYTHON_EXE ?? "python";

function json(path) {
  return JSON.parse(readFileSync(resolve(PROJECT_ROOT, path), "utf8"));
}

test("all contract documents, references and examples validate", () => {
  const summary = validateContracts();
  assert.equal(summary.operations, 33, "operation additions/removals require an explicit contract version change");
  assert.equal(summary.events, 25, "event additions/removals require an explicit contract version change");
  assert.equal(summary.errors, 26, "error additions/removals require an explicit contract version change");
  assert.equal(summary.examples, 64, "every frozen example must remain in the validation set");
  assert.ok(summary.files >= 72, `expected the complete contract surface, got ${summary.files} JSON files`);
  assert.ok(summary.refs >= 100, `schemas are not sharing common contracts: ${summary.refs}`);
});

test("skill source bundles publish the runtime integrity invariants and the frozen example satisfies them", () => {
  const schema = json("contracts/schemas/game/skill-build-create-request.schema.json");
  assert.deepEqual(schema.properties.source_bundle["x-invariants"], [
    "every files[*].content_sha256 equals lowercase SHA-256 of the UTF-8 bytes of files[*].content",
    "files[*].path values are unique",
    "entrypoint equals exactly one files[*].path",
    "files.length <= bootstrap.limits.max_source_files == 32",
    "sum(utf8_byte_length(files[*].content)) <= bootstrap.limits.max_source_bytes == 1048576",
  ]);
  const bundle = json("contracts/examples/game-skill-build-create-request.json").value.source_bundle;
  assert.equal(new Set(bundle.files.map((file) => file.path)).size, bundle.files.length);
  assert.equal(bundle.files.filter((file) => file.path === bundle.entrypoint).length, 1);
  for (const file of bundle.files) {
    assert.equal(
      file.content_sha256,
      createHash("sha256").update(file.content, "utf8").digest("hex"),
      `${file.path} content hash drifted`,
    );
  }
});

test("bootstrap source limits are frozen and the create contract exposes the same boundary", () => {
  const bootstrapSchema = json("contracts/schemas/game/bootstrap-response.schema.json");
  const requestSchema = json("contracts/schemas/game/skill-build-create-request.schema.json");
  const bootstrap = json("contracts/examples/game-bootstrap-response.json").value;
  assert.equal(bootstrapSchema.properties.limits.properties.max_source_files.const, 32);
  assert.equal(bootstrapSchema.properties.limits.properties.max_source_bytes.const, 1_048_576);
  assert.equal(bootstrap.limits.max_source_files, 32);
  assert.equal(bootstrap.limits.max_source_bytes, 1_048_576);
  assert.equal(requestSchema.properties.source_bundle.properties.files.maxItems, 32);
});

test("Node and mock RFC3339 validation matches Python and Godot edge semantics", () => {
  const schema = { type: "string", format: "date-time" };
  const positive = [
    "2026-08-07T10:00:00Z",
    "2026-08-07t10:00:00z",
    "2026-08-07T10:00:00+08:00",
    "2026-08-07T10:00:00-00:00",
    "2024-02-29T23:59:59.123456789-03:30",
  ];
  const negative = [
    "20260807T100000Z",
    "2026-08-07 10:00:00Z",
    "2026-08-07T10:00Z",
    "2026-08-07T10:00:00",
    "2026-02-29T10:00:00Z",
    "2026-04-31T10:00:00Z",
    "2026-13-07T10:00:00Z",
    "2026-08-32T10:00:00Z",
    "2026-08-07T24:00:00Z",
    "2026-08-07T23:59:60Z",
    "2026-08-07T10:00:00+24:00",
    "2026-08-07T10:00:00+23:60",
    "2026-08-07T10:00:00+0800",
    "2026-08-07T10:00:00+08:00:30",
    "0000-01-01T00:00:00Z",
    "２０２６-08-07T10:00:00Z",
  ];
  for (const value of positive) {
    assert.doesNotThrow(() => assertSchema(value, schema, PROJECT_ROOT, new Map()), value);
  }
  for (const value of negative) {
    assert.throws(() => assertSchema(value, schema, PROJECT_ROOT, new Map()), undefined, value);
  }
});

test("custom JSON Schema equality and string lengths use the JSON data model", () => {
  const documents = new Map();
  assert.doesNotThrow(() => assertSchema(
    { b: [2], a: 1 },
    { const: { a: 1, b: [2] } },
    PROJECT_ROOT,
    documents,
  ));
  assert.doesNotThrow(() => assertSchema(
    [1, 2],
    { enum: [{ a: 1 }, [1, 2]] },
    PROJECT_ROOT,
    documents,
  ));
  assert.doesNotThrow(() => assertSchema(JSON.parse("-0"), { const: 0 }, PROJECT_ROOT, documents));
  assert.throws(() => assertSchema(
    [{ a: 1, b: 2 }, { b: 2, a: 1 }],
    { type: "array", uniqueItems: true },
    PROJECT_ROOT,
    documents,
  ));
  assert.doesNotThrow(() => assertSchema(
    [{ a: 1 }, { a: 2 }],
    { type: "array", uniqueItems: true },
    PROJECT_ROOT,
    documents,
  ));

  const emoji = String.fromCodePoint(0x1f600);
  assert.doesNotThrow(() => assertSchema(
    emoji,
    { type: "string", minLength: 1, maxLength: 1 },
    PROJECT_ROOT,
    documents,
  ));
  assert.throws(() => assertSchema(
    emoji,
    { type: "string", minLength: 2 },
    PROJECT_ROOT,
    documents,
  ));
});

test("URI and URI-reference formats match Draft 2020-12 RFC3986 semantics", () => {
  const emoji = String.fromCodePoint(0x1f600);
  const unicodeHost = `https://${String.fromCodePoint(0x4f8b)}.${String.fromCodePoint(0x6d4b)}/`;
  const cases = [
    { format: "uri", value: "http://example.com", expected: true },
    { format: "uri", value: "http://", expected: true },
    { format: "uri", value: "urn:isbn:0451450523", expected: true },
    { format: "uri", value: "mailto:user@example.com", expected: true },
    { format: "uri", value: "https://example.com/a%20b?q=x#fragment", expected: true },
    { format: "uri", value: "http:\\example.com", expected: false },
    { format: "uri", value: "http://example.com/%zz", expected: false },
    { format: "uri", value: "http://example.com/[", expected: false },
    { format: "uri", value: "http://example.com/|", expected: false },
    { format: "uri", value: `http://example.com/${emoji}`, expected: false },
    { format: "uri", value: unicodeHost, expected: false },
    { format: "uri-reference", value: "", expected: true },
    { format: "uri-reference", value: "../worlds/1", expected: true },
    { format: "uri-reference", value: "/v1/worlds/1?cursor=2#events", expected: true },
    { format: "uri-reference", value: "//example.com/path", expected: true },
    { format: "uri-reference", value: "resource%20name", expected: true },
    { format: "uri-reference", value: "%zz", expected: false },
    { format: "uri-reference", value: "[", expected: false },
    { format: "uri-reference", value: "a\\b", expected: false },
    { format: "uri-reference", value: "a|b", expected: false },
    { format: "uri-reference", value: "a{b}", expected: false },
    { format: "uri-reference", value: "a^b", expected: false },
    { format: "uri-reference", value: "://bad", expected: false },
    { format: "uri-reference", value: "1http://x", expected: false },
  ];
  const actual = cases.map(({ format, value }) => {
    try {
      assertSchema(value, { type: "string", format }, PROJECT_ROOT, new Map());
      return true;
    } catch {
      return false;
    }
  });
  assert.deepEqual(actual, cases.map(({ expected }) => expected));

  const standardValidation = spawnSync(PYTHON_EXE, ["-c", String.raw`
import json
import sys
from jsonschema import Draft202012Validator, FormatChecker

checker = FormatChecker()
if checker.conforms("http://example.com/%zz", "uri") or checker.conforms("%zz", "uri-reference"):
    raise RuntimeError("RFC3986 format validation is unavailable; install rfc3986-validator")
cases = json.load(sys.stdin)
results = []
for case in cases:
    schema = {"type": "string", "format": case["format"]}
    results.append(Draft202012Validator(schema, format_checker=checker).is_valid(case["value"]))
print(json.dumps(results))
`], {
    encoding: "utf8",
    env: { ...process.env, PYTHONUTF8: "1" },
    input: JSON.stringify(cases),
  });
  assert.equal(standardValidation.status, 0, standardValidation.stderr || standardValidation.stdout);
  assert.deepEqual(actual, JSON.parse(standardValidation.stdout));
});

test("command evidence references cap cardinality and enforce evidence_id uniqueness", () => {
  const schemaPath = resolve(PROJECT_ROOT, "contracts/schemas/game/command.schema.json");
  const { documents } = loadDocuments();
  const schema = documents.get(schemaPath);
  const evidenceRefsSchema = schema.properties.evidence_refs;
  assert.equal(evidenceRefsSchema.maxItems, 64);
  assert.deepEqual(evidenceRefsSchema["x-invariants"], [
    "evidence_refs contains at most one immutable reference for each evidence_id",
  ]);

  const command = json("contracts/examples/game-command.json").value;
  const missingRunLink = structuredClone(command);
  delete missingRunLink.links.run;
  assert.throws(
    () => assertSchema(missingRunLink, schema, schemaPath, documents),
    /links\.run is required/u,
  );
  const noEffectWithoutRun = structuredClone(command);
  noEffectWithoutRun.result = { result_type: "NO_EFFECT", reason_code: "NO_EFFECT" };
  delete noEffectWithoutRun.links.run;
  delete noEffectWithoutRun.links.world_snapshot;
  assert.doesNotThrow(() => assertSchema(noEffectWithoutRun, schema, schemaPath, documents));
  const runningWithoutRunLink = structuredClone(command);
  Object.assign(runningWithoutRunLink, {
    status: "RUNNING_SANDBOX",
    stage: "SANDBOX",
    terminal: false,
    result: null,
    error: null,
  });
  delete runningWithoutRunLink.links.run;
  assert.throws(
    () => assertSchema(runningWithoutRunLink, schema, schemaPath, documents),
    /links\.run is required/u,
  );
  const failedWithoutRunLink = structuredClone(command);
  Object.assign(failedWithoutRunLink, {
    status: "FAILED",
    stage: "SANDBOX",
    terminal: true,
    result: null,
    error: {
      code: "SANDBOX_RUNTIME_ERROR",
      category: "SANDBOX",
      retryable: false,
      user_message_key: "sandbox.runtime_error",
      stage: "SANDBOX",
    },
  });
  delete failedWithoutRunLink.links.run;
  assert.throws(
    () => assertSchema(failedWithoutRunLink, schema, schemaPath, documents),
    /links\.run is required/u,
  );
  const duplicateId = structuredClone(command);
  duplicateId.evidence_refs.push({
    ...duplicateId.evidence_refs[0],
    uri: "/v1/evidence/evidence_world_00000001",
  });
  assert.throws(
    () => assertSchema(duplicateId, schema, schemaPath, documents),
    /unique evidence_id values/u,
  );

  const maximum = structuredClone(command);
  maximum.evidence_refs = Array.from({ length: 64 }, (_, index) => ({
    ...maximum.evidence_refs[0],
    evidence_id: `evidence_command_${String(index).padStart(8, "0")}`,
  }));
  assert.doesNotThrow(() => assertSchema(maximum, schema, schemaPath, documents));
  maximum.evidence_refs.push({
    ...maximum.evidence_refs[0],
    evidence_id: "evidence_command_overflow",
  });
  assert.throws(
    () => assertSchema(maximum, schema, schemaPath, documents),
    /too many items/u,
  );
});

test("Feishu response trace IDs use the canonical trace contract", () => {
  const expected = { type: "string", pattern: "^trace_[A-Za-z0-9_-]{8,96}$" };
  const schemas = [
    json("contracts/schemas/feishu/approval-decision-receipt.schema.json").properties.trace_id,
    json("contracts/schemas/feishu/class-insights-result.schema.json").properties.trace_id,
    json("contracts/schemas/feishu/content-release-receipt.schema.json").properties.trace_id,
    json("contracts/schemas/feishu/webhook-response.schema.json").oneOf[1].properties.trace_id,
    json("contracts/schemas/feishu/learner-query-result.schema.json").properties.trace_id,
    json("contracts/schemas/feishu/evidence-view.schema.json").properties.trace_id,
  ];
  for (const traceSchema of schemas) {
    assert.deepEqual(traceSchema, expected);
    assert.doesNotThrow(() => assertSchema(
      "trace_feishu_00000001", traceSchema, PROJECT_ROOT, new Map(),
    ));
    for (const invalid of ["bad", "trace_short", "trace_has.dot_0001", `trace_${"a".repeat(97)}`]) {
      assert.throws(() => assertSchema(invalid, traceSchema, PROJECT_ROOT, new Map()));
    }
  }
});

test("the HTTP operation list is exact and cannot silently lose an endpoint", () => {
  const expected = [
    "activateSkillVersion",
    "createAgentSession",
    "createAgentTurn",
    "createFeishuContentReleaseCandidate",
    "createFeishuReportDraftJob",
    "createSkillBuild",
    "getCommand",
    "getEvidence",
    "getFeishuContentReleaseStatus",
    "getFeishuReportDraftJob",
    "getGameBootstrap",
    "getProductAgentInteraction",
    "getProductContentUnit",
    "getProductSessionWorkspace",
    "getProductSkillDraft",
    "getRedactedEvidenceForFeishu",
    "getRun",
    "getSkillActivation",
    "getSkillBuild",
    "getAgentSession",
    "getWorldSnapshot",
    "ingestClientEventBatch",
    "listWorldEvents",
    "listProductAgentInteractions",
    "queryClassInsightsFromFeishu",
    "queryLearnerProjectionFromFeishu",
    "receiveFeishuWebhook",
    "recordFeishuApprovalDecision",
    "recordProductPatchDecision",
    "upsertProductSkillDraft",
  ].sort();
  const actual = [];
  for (const contractName of [
    "game-api.openapi.json",
    "feishu-integration.openapi.json",
    "product-experience.openapi.json",
  ]) {
    const contract = json(`contracts/openapi/${contractName}`);
    for (const pathItem of Object.values(contract.paths)) {
      for (const operation of Object.values(pathItem)) {
        if (operation?.operationId) actual.push(operation.operationId);
      }
    }
  }
  assert.deepEqual(actual.sort(), expected);
});

test("every Game operation publishes closed current-attempt headers separately from origin context", () => {
  const game = json("contracts/openapi/game-api.openapi.json");
  const commonParameters = ["RequestId", "TraceId", "CorrelationId", "SchemaVersion"]
    .map((name) => `#/components/parameters/${name}`);
  const currentAttemptHeaders = ["X-Request-Id", "X-Trace-Id", "X-Correlation-Id"];
  const resolveLocal = (candidate) => {
    let value = candidate;
    const seen = new Set();
    while (value?.$ref?.startsWith("#/")) {
      assert.ok(!seen.has(value.$ref), `cyclic OpenAPI response reference ${value.$ref}`);
      seen.add(value.$ref);
      value = value.$ref.slice(2).split("/").reduce((node, segment) => node[segment], game);
    }
    return value;
  };

  for (const pathItem of Object.values(game.paths)) {
    for (const operation of Object.values(pathItem)) {
      if (!operation?.operationId) continue;
      const refs = operation.parameters.map((parameter) => parameter.$ref);
      for (const ref of commonParameters) {
        assert.ok(refs.includes(ref), `${operation.operationId} hides ${ref.split("/").at(-1)}`);
      }
      for (const [status, response] of Object.entries(operation.responses)) {
        const resolved = resolveLocal(response);
        for (const header of currentAttemptHeaders) {
          assert.ok(
            resolved.headers?.[header],
            `${operation.operationId} HTTP ${status} does not publish ${header}`,
          );
        }
      }
    }
  }

  for (const parameter of [game.components.parameters.TraceId, game.components.parameters.CorrelationId]) {
    assert.equal(parameter.in, "header");
    assert.equal(parameter.required, true);
  }
  assert.deepEqual(
    game.components.parameters.TraceId.schema,
    { type: "string", pattern: "^trace_[A-Za-z0-9_-]{8,96}$" },
  );
  assert.deepEqual(
    game.components.parameters.CorrelationId.schema,
    { type: "string", pattern: "^corr_[A-Za-z0-9_-]{8,96}$" },
  );

  const requestContext = json("contracts/schemas/common/request-context.schema.json");
  assert.deepEqual(requestContext["x-invariants"], [
    "persisted resource request_context is immutable origin context",
    "current HTTP attempt identity is transported separately and never overwrites origin context",
  ]);
  assert.deepEqual(
    requestContext.$defs.wireAttemptContext.required,
    ["schema_version", "request_id", "trace_id", "correlation_id"],
  );
  assert.equal(requestContext.$defs.wireAttemptContext.additionalProperties, false);
  assert.ok(!requestContext.$defs.wireAttemptContext.properties.actor);
  assert.ok(!requestContext.$defs.wireAttemptContext.properties.content_ref);
});

test("Game authentication separates production JWTs from deterministic Mock credentials", () => {
  const game = json("contracts/openapi/game-api.openapi.json");
  const scheme = game.components.securitySchemes.bearerAuth;
  assert.equal(scheme.type, "http");
  assert.equal(scheme.scheme, "bearer");
  assert.equal(scheme.bearerFormat, "JWT");
  assert.match(scheme.description, /Production profile/u);
  assert.match(scheme.description, /local Mock profile only/u);
  assert.equal(scheme["x-development-profile"].server, "http://127.0.0.1:8790");
  assert.deepEqual(
    scheme["x-development-profile"].loopback_host_aliases,
    ["127.0.0.1", "localhost"],
  );
  assert.equal(scheme["x-development-profile"].production_allowed, false);
  assert.equal(
    scheme["x-development-profile"].token_pattern,
    "^[A-Za-z0-9_-]{3,96}:[A-Za-z0-9_-]{3,128}$",
  );
});

test("the event message list and correlation pointer are exact", () => {
  const expectedMessages = [
    "CommandAccepted", "CommandStageChanged", "CommandTerminal",
    "AgentTurnFeedbackReady",
    "SkillBuildRequested", "SkillBuildStarted", "SkillBuildCompleted", "SkillBuildFailed",
    "SkillCertificationGranted", "SkillCertificationRejected",
    "SkillActivationApplied", "SkillActivationRejected",
    "SandboxRunStarted", "SandboxRunCompleted", "SandboxRunFailed",
    "WorldCommitted", "WorldRejected",
    "LearnerEvidenceRecorded", "LearnerInferenceRecorded", "LearnerModelUpdated",
    "LearnerProjectionFailed",
    "FeishuSyncRequested", "FeishuSyncSucceeded", "FeishuSyncFailed", "FeishuSyncDeadLettered",
  ].sort();
  const contract = json("contracts/asyncapi/runtime-events.asyncapi.json");
  assert.deepEqual(Object.keys(contract.components.messages).sort(), expectedMessages);
  for (const [name, message] of Object.entries(contract.components.messages)) {
    assert.equal(message.correlationId?.location, "$message.payload#/correlation_id",
      `${name} must propagate correlation_id`);
  }
});

test("runtime EvidenceRefs cardinality is identical across contract and adapters", () => {
  const asyncApi = json("contracts/asyncapi/runtime-events.asyncapi.json");
  assert.deepEqual(
    {
      maxItems: asyncApi.components.schemas.EvidenceRefs.maxItems,
      uniqueItems: asyncApi.components.schemas.EvidenceRefs.uniqueItems,
    },
    { maxItems: 64, uniqueItems: true },
  );
  const python = readFileSync(resolve(PROJECT_ROOT, "python/yaya_agent_contracts/models.py"), "utf8");
  const godot = readFileSync(resolve(PROJECT_ROOT, "clients/godot/contract_validator.gd"), "utf8");
  assert.match(python, /_require_unique_array\(value, field_name, 64\)/u);
  assert.match(godot, /value\.size\(\) > 64/u);
  assert.match(godot, /must contain unique evidence_id values/u);

  const asyncApiPath = resolve(PROJECT_ROOT, "contracts/asyncapi/runtime-events.asyncapi.json");
  const { documents } = loadDocuments();
  const evidenceRefsSchema = documents.get(asyncApiPath).components.schemas.EvidenceRefs;
  const evidence = (index) => ({
    evidence_id: `evidence_cardinality_${String(index).padStart(8, "0")}`,
    evidence_type: "TEST_REPORT",
    created_at: "2026-08-07T10:00:00Z",
    sha256: String(index % 10).repeat(64),
  });
  assert.doesNotThrow(() => assertSchema(
    Array.from({ length: 64 }, (_, index) => evidence(index)),
    evidenceRefsSchema,
    asyncApiPath,
    documents,
  ));
  assert.throws(() => assertSchema(
    Array.from({ length: 65 }, (_, index) => evidence(index)),
    evidenceRefsSchema,
    asyncApiPath,
    documents,
  ));
  assert.throws(() => assertSchema(
    [evidence(1), evidence(1)],
    evidenceRefsSchema,
    asyncApiPath,
    documents,
  ));
});

test("the error catalog and binding schema expose exactly the same codes", () => {
  const expected = [
    "INVALID_REQUEST", "SCHEMA_VERSION_UNSUPPORTED", "CONTENT_VERSION_MISMATCH",
    "AUTHENTICATION_REQUIRED", "AUTHORIZATION_DENIED", "POLICY_DENIED", "NOT_FOUND",
    "PAYLOAD_TOO_LARGE", "IDEMPOTENCY_KEY_REUSED", "WORLD_REVISION_CONFLICT",
    "EVENT_SEQUENCE_GAP", "SKILL_NOT_CERTIFIED", "SKILL_VERSION_MISMATCH",
    "ACTIVE_SKILL_ARTIFACT_MISMATCH", "SANDBOX_COMPILE_ERROR", "SANDBOX_RUNTIME_ERROR",
    "SANDBOX_RESOURCE_LIMIT", "WORLD_RULE_REJECTED", "DEPENDENCY_UNAVAILABLE",
    "FEISHU_SIGNATURE_INVALID", "FEISHU_REPLAY_DETECTED", "FEISHU_SYNC_FAILED",
    "RATE_LIMITED", "UNKNOWN_COMMIT_STATE", "INVARIANT_VIOLATION", "INTERNAL_ERROR",
  ].sort();
  const catalog = json("contracts/error-catalog.json");
  const errorSchema = json("contracts/schemas/common/error.schema.json");
  const errorCodeSchema = json("contracts/schemas/common/error-code.schema.json");
  const auditSchema = json("contracts/schemas/common/audit-record.schema.json");
  assert.deepEqual(catalog.errors.map((entry) => entry.code).sort(), expected);
  assert.deepEqual(Object.keys(errorSchema.$defs).sort(), expected);
  assert.deepEqual([...errorCodeSchema.enum].sort(), expected);
  assert.equal(errorSchema.properties.code.$ref, "./error-code.schema.json");
  assert.ok(auditSchema.properties.error_code.oneOf.some(
    (branch) => branch.$ref === "./error-code.schema.json",
  ));
});

test("OpenAPI error bodies bind catalog codes to their HTTP status", () => {
  const catalog = json("contracts/error-catalog.json");
  const statusSchema = json("contracts/schemas/common/error-responses-by-status.schema.json");
  const grouped = new Map();
  for (const entry of catalog.errors) {
    const status = String(entry.http_status);
    grouped.set(status, [...(grouped.get(status) ?? []), entry.code]);
  }
  for (const [status, codes] of grouped) {
    assert.deepEqual(
      [...statusSchema.$defs[`error${status}`].properties.code.enum].sort(),
      [...codes].sort(),
      `HTTP ${status} code set drifted from the catalog`,
    );
  }

  const game = json("contracts/openapi/game-api.openapi.json");
  for (const status of grouped.keys()) {
    const ref = game.components.responses[`Error${status}`]
      .content["application/json"].schema.$ref;
    assert.equal(ref, `../schemas/common/error-responses-by-status.schema.json#/$defs/status${status}`);
  }
  const feishu = json("contracts/openapi/feishu-integration.openapi.json");
  const feishuResponses = {
    BadRequest: "400", InvalidSignature: "401", Unauthorized: "401", Forbidden: "403",
    NotFound: "404", Conflict: "409", Unprocessable: "422", RateLimited: "429",
    InternalError: "500", ServiceUnavailable: "503",
  };
  for (const [name, status] of Object.entries(feishuResponses)) {
    assert.equal(
      feishu.components.responses[name].content["application/json"].schema.$ref,
      `../schemas/common/error-responses-by-status.schema.json#/$defs/status${status}`,
    );
    assert.ok(feishu.components.responses[name].headers?.["X-Request-Id"]);
    assert.ok(feishu.components.responses[name].headers?.["X-Trace-Id"]);
  }
  for (const specification of [game, feishu]) {
    const rateLimited = specification === game
      ? specification.components.responses.Error429
      : specification.components.responses.RateLimited;
    const unavailable = specification === game
      ? specification.components.responses.Error503
      : specification.components.responses.ServiceUnavailable;
    assert.ok(rateLimited.headers?.["Retry-After"], "429 must declare Retry-After");
    assert.ok(unavailable.headers?.["Retry-After"], "503 must declare Retry-After");
  }
});

test("command status distinguishes accepted, applied, failed and unknown", () => {
  const schema = json("contracts/schemas/common/command-status.schema.json");
  for (const status of ["ACCEPTED", "APPLIED", "REJECTED", "FAILED", "UNKNOWN"]) {
    assert.ok(schema.enum.includes(status), `missing command status ${status}`);
  }
  assert.ok(!schema.enum.includes("SUCCESS"), "ambiguous SUCCESS status must not be used");
});

test("WORLD_COMMIT evidence declares the executable +1 revision invariant", () => {
  const schema = json("contracts/schemas/game/evidence.schema.json");
  assert.deepEqual(schema.$defs.worldCommitEvidence["x-invariants"], [
    "world_revision == previous_revision + 1",
    "first_event_sequence <= last_event_sequence",
  ]);
});

test("all successful revision events publish their executable +1 invariant", () => {
  const asyncapi = json("contracts/asyncapi/runtime-events.asyncapi.json");
  const schemas = asyncapi.components.schemas;
  assert.deepEqual(
    schemas.SkillActivationAppliedPayload["x-invariants"],
    ["registry_revision = previous_registry_revision + 1"],
  );
  assert.deepEqual(
    schemas.LearnerModelUpdatedPayload["x-invariants"],
    ["learner_revision = previous_revision + 1"],
  );
  assert.deepEqual(
    schemas.CommandStageChangedPayload["x-invariants"],
    [
      "to_status != from_status",
      "to_status is a legal successor of from_status according to the CommandStatus graph",
    ],
  );
});

test("Python non-empty and diagnostic constraints are published in wire schemas", () => {
  const versionSet = json("contracts/schemas/common/version-set.schema.json");
  for (const field of [
    "skill_version",
    "compiler_version",
    "sandbox_image_digest",
    "test_suite_version",
    "prompt_version",
    "model_version",
  ]) {
    assert.equal(versionSet.properties[field].minLength, 1, field);
  }
  assert.equal(
    json("contracts/schemas/common/evidence-ref.schema.json").properties.uri.minLength,
    1,
  );
  assert.equal(
    json("contracts/schemas/common/error.schema.json").properties.message.minLength,
    1,
  );
  const asyncDiagnostics = json("contracts/asyncapi/runtime-events.asyncapi.json")
    .components.schemas.TestCaseResult.properties.diagnostic_codes;
  const gameDiagnostics = json("contracts/schemas/game/skill-build.schema.json")
    .properties.phases.items.properties.diagnostic_codes;
  for (const diagnostics of [asyncDiagnostics, gameDiagnostics]) {
    assert.equal(diagnostics.maxItems, 100);
    assert.equal(diagnostics.uniqueItems, true);
    assert.equal(diagnostics.items.minLength, 1);
    assert.equal(diagnostics.items.maxLength, 96);
  }
});

test("accepted game jobs require reconciliation identities and reject execution states", () => {
  const schemaPath = resolve(PROJECT_ROOT, "contracts/schemas/game/accepted-game-job.schema.json");
  const { documents } = loadDocuments();
  const schema = documents.get(schemaPath);
  assert.deepEqual(schema["x-invariants"], [
    "trace_id is the immutable original command trace and may differ from the current HTTP attempt trace only when Idempotency-Replayed is true",
    "X-Request-Id and X-Trace-Id response headers always identify the current HTTP attempt",
  ]);
  const gameOpenApi = json("contracts/openapi/game-api.openapi.json");
  assert.ok(gameOpenApi.components.responses.AcceptedJob.headers["Idempotency-Replayed"]);
  const valid = {
    job_id: "job_accepted_00000001",
    job_type: "CREATE_SKILL_BUILD",
    status: "ACCEPTED",
    created_at: "2026-08-06T10:00:00Z",
    updated_at: "2026-08-06T10:00:00Z",
    command_id: "cmd_accepted_00000001",
    trace_id: "trace_accepted_00000001",
    error: null,
  };
  assert.doesNotThrow(() => assertSchema(valid, schema, schemaPath, documents));
  const invalid = [
    { ...valid, command_id: undefined },
    { ...valid, trace_id: undefined },
    { ...valid, status: "RUNNING" },
    { ...valid, status: "SUCCEEDED" },
  ];
  for (const mutation of invalid) {
    for (const key of ["command_id", "trace_id"]) {
      if (mutation[key] === undefined) delete mutation[key];
    }
    assert.throws(() => assertSchema(mutation, schema, schemaPath, documents));
  }

  const standardValidation = spawnSync(PYTHON_EXE, ["-c", String.raw`
import json
from pathlib import Path
import sys
from jsonschema.validators import validator_for
from referencing import Registry, Resource

root = Path(sys.argv[1])
schema_root = root / "contracts" / "schemas"
schemas = [json.loads(path.read_text(encoding="utf-8")) for path in schema_root.rglob("*.schema.json")]
registry = Registry()
for candidate in schemas:
    registry = registry.with_resource(candidate["$id"], Resource.from_contents(candidate))
schema = json.loads((schema_root / "game" / "accepted-game-job.schema.json").read_text(encoding="utf-8"))
validator = validator_for(schema)(schema, registry=registry)
cases = json.load(sys.stdin)
if list(validator.iter_errors(cases["valid"])):
    raise SystemExit("valid accepted game job was rejected")
for mutation in cases["invalid"]:
    if not list(validator.iter_errors(mutation)):
        raise SystemExit("invalid accepted game job passed standard JSON Schema validation")
`, PROJECT_ROOT], {
    encoding: "utf8",
    input: JSON.stringify({ valid, invalid }),
  });
  assert.equal(standardValidation.status, 0, standardValidation.stderr || standardValidation.stdout);
});

test("Feishu evidence purpose enum is the exact mock authorization vocabulary", () => {
  const openapi = json("contracts/openapi/feishu-integration.openapi.json");
  assert.deepEqual(openapi.components.parameters.EvidencePurpose.schema.enum, [
    "TEACHER_SUPPORT",
    "GUARDIAN_REPORT",
    "LEARNING_REVIEW",
    "SAFETY_INVESTIGATION",
  ]);
  assert.equal(
    openapi.paths["/integrations/feishu/v1/evidence/{evidence_id}"].get.responses["400"].$ref,
    "#/components/responses/BadRequest",
  );
});

test("Feishu approval publishes one authoritative candidate revision CAS", () => {
  const openapi = json("contracts/openapi/feishu-integration.openapi.json");
  const operation = openapi.paths["/integrations/feishu/v1/approval-decisions"].post;
  assert.deepEqual(operation["x-invariants"], [
    "request.expected_candidate_revision == current candidate_revision",
    "a newly recorded decision sets response.candidate_revision == request.expected_candidate_revision + 1",
    "tenant_id + approval_instance_id identifies one immutable canonical business decision and receipt",
    "an identical canonical business decision returns the original receipt without another revision increment",
    "conflicting approval-instance reuse and stale decisions return CONTENT_VERSION_MISMATCH without mutation",
    "WORKFLOW_CLOSED rejects later approval instances for the candidate without mutation",
  ]);
  assert.match(operation.description, /409 IDEMPOTENCY_KEY_REUSED/u);
  assert.match(operation.description, /409 CONTENT_VERSION_MISMATCH/u);

  const creation = json("contracts/examples/feishu-content-release-response.json").value;
  const status = json("contracts/examples/feishu-content-release-status-response.json").value;
  const approval = json("contracts/examples/feishu-approval-decision-response.json").value;
  assert.equal(creation.candidate_revision, 1);
  assert.equal(status.candidate_revision, creation.candidate_revision);
  assert.equal(approval.candidate_revision, creation.candidate_revision + 1);

  for (const [schemaName, value] of [
    ["content-release-receipt.schema.json", creation],
    ["content-release-status.schema.json", status],
  ]) {
    const schemaPath = resolve(PROJECT_ROOT, "contracts", "schemas", "feishu", schemaName);
    const { documents } = loadDocuments();
    const schema = documents.get(schemaPath);
    const missingRevision = structuredClone(value);
    delete missingRevision.candidate_revision;
    assert.throws(() => assertSchema(missingRevision, schema, schemaPath, documents));
  }
});

test("every asynchronous 202 response declares its idempotency replay identity", () => {
  const feishu = json("contracts/openapi/feishu-integration.openapi.json");
  for (const path of [
    "/integrations/feishu/v1/content-releases",
    "/integrations/feishu/v1/report-jobs",
  ]) {
    assert.ok(feishu.paths[path].post.responses["202"].headers["Idempotency-Replayed"]);
  }
  assert.deepEqual(feishu.components.headers.IdempotencyReplayed.schema.enum, ["false", "true"]);
});

test("Feishu mock role policies cannot drift from OpenAPI", () => {
  const openapi = json("contracts/openapi/feishu-integration.openapi.json");
  const expected = new Map();
  for (const [path, pathItem] of Object.entries(openapi.paths)) {
    for (const [method, operation] of Object.entries(pathItem)) {
      if (operation["x-required-role"]) {
        expected.set(`${method.toUpperCase()} ${path}`, operation["x-required-role"]);
      }
    }
  }
  assert.deepEqual(MOCK_FEISHU_ROLE_POLICIES, expected);
});

test("event envelope pins identity, ordering and causal tracing", () => {
  const schema = json("contracts/schemas/common/event-envelope.schema.json");
  for (const field of [
    "event_id", "event_type", "event_version", "stream_id", "sequence",
    "trace_id", "command_id", "correlation_id", "causation_id", "content_ref",
  ]) {
    assert.ok(schema.required.includes(field), `event envelope must require ${field}`);
  }
});

test("version set contains every deterministic replay boundary", () => {
  const schema = json("contracts/schemas/common/version-set.schema.json");
  for (const field of [
    "policy_version", "world_rules_version", "teaching_spec_version",
    "artifact_sha256", "compiler_version", "test_suite_version", "prompt_version", "model_version",
  ]) {
    assert.ok(schema.properties[field], `version set must define ${field}`);
  }
});

test("cross-document world commit contracts declare the +1 revision invariant", () => {
  const command = json("contracts/schemas/game/command.schema.json");
  const run = json("contracts/schemas/game/run.schema.json");
  const asyncApi = json("contracts/asyncapi/runtime-events.asyncapi.json");
  assert.match(command["x-invariants"].join(" "), /previous_revision \+ 1/u);
  assert.match(run["x-invariants"].join(" "), /previous_revision \+ 1/u);
  assert.match(
    asyncApi.components.schemas.WorldCommittedPayload["x-invariants"].join(" "),
    /previous_world_revision \+ 1/u,
  );
});
