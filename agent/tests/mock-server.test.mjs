import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  createMockServer,
  signMockFeishuBody,
} from "../scripts/mock-server.mjs";
import { assertSchema, loadDocuments } from "../scripts/validate-contracts.mjs";

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const AGENT_ROOT = resolve(TEST_DIR, "..");
const FIXED_NOW = Date.parse("2026-08-06T10:00:00Z");
const FIXED_TIMESTAMP = String(Math.floor(FIXED_NOW / 1000));
const FEISHU_SECRET = "test-feishu-contract-secret";

function example(name) {
  return JSON.parse(readFileSync(resolve(AGENT_ROOT, "contracts", "examples", name), "utf8")).value;
}

function utf8Sha256(content) {
  return createHash("sha256").update(content, "utf8").digest("hex");
}

async function listen(server) {
  await new Promise((resolvePromise, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolvePromise);
  });
  const address = server.address();
  return `http://127.0.0.1:${address.port}`;
}

async function close(server) {
  server.closeAllConnections();
  await new Promise((resolvePromise) => server.close(resolvePromise));
}

const readHeaders = {
  Authorization: "Bearer tenant_yaya:student_0001",
  "X-Request-Id": "req_mock_00000001",
  "X-Trace-Id": "trace_mock_00000001",
  "X-Correlation-Id": "corr_mock_00000001",
  "X-Schema-Version": "1.0.0",
};

const writeHeaders = {
  ...readHeaders,
  "Content-Type": "application/json",
  "Idempotency-Key": "idem_mock_00000001",
};

function feishuHeaders(rawBody, nonce, overrides = {}) {
  const timestamp = overrides.timestamp ?? FIXED_TIMESTAMP;
  let context;
  try {
    context = JSON.parse(rawBody).context;
  } catch {
    context = undefined;
  }
  return {
    Authorization: overrides.authorization
      ?? `Bearer ${context?.actor?.tenant_id ?? "tenant_yaya"}:${context?.actor?.actor_id ?? "feishu_operator_001"}`,
    "Content-Type": "application/json",
    "X-Request-Id": overrides.requestId ?? context?.request_id ?? "req_feishu_00000001",
    "X-Trace-Id": overrides.traceId ?? context?.trace_id ?? "trace_feishu_00000001",
    "X-Schema-Version": "1.0.0",
    "Idempotency-Key": overrides.idempotencyKey ?? `idem_${nonce}`,
    "X-Lark-Request-Timestamp": timestamp,
    "X-Lark-Request-Nonce": nonce,
    "X-Lark-Signature": signMockFeishuBody(rawBody, timestamp, nonce, FEISHU_SECRET),
  };
}

test("mock server validates real request schemas and returns the same reconciled command", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  try {
    const health = await fetch(`${baseUrl}/health`).then((response) => response.json());
    assert.equal(health.mode, "contract_mock");

    const missingRequestId = await fetch(`${baseUrl}/v1/bootstrap`, {
      headers: { Authorization: readHeaders.Authorization, "X-Schema-Version": "1.0.0" },
    });
    assert.equal(missingRequestId.status, 400);
    assert.equal((await missingRequestId.json()).error.code, "INVALID_REQUEST");

    const missingSchemaVersion = await fetch(`${baseUrl}/v1/bootstrap`, {
      headers: { Authorization: readHeaders.Authorization, "X-Request-Id": readHeaders["X-Request-Id"] },
    });
    assert.equal(missingSchemaVersion.status, 400);

    const missingTraceId = await fetch(`${baseUrl}/v1/bootstrap`, {
      headers: {
        Authorization: readHeaders.Authorization,
        "X-Request-Id": readHeaders["X-Request-Id"],
        "X-Correlation-Id": readHeaders["X-Correlation-Id"],
        "X-Schema-Version": readHeaders["X-Schema-Version"],
      },
    });
    assert.equal(missingTraceId.status, 400);
    assert.equal((await missingTraceId.json()).error.details.missing_header, "X-Trace-Id");

    const missingCorrelationId = await fetch(`${baseUrl}/v1/bootstrap`, {
      headers: Object.fromEntries(
        Object.entries(readHeaders).filter(([name]) => name !== "X-Correlation-Id"),
      ),
    });
    assert.equal(missingCorrelationId.status, 400);
    assert.equal((await missingCorrelationId.json()).error.details.missing_header, "X-Correlation-Id");
    assert.match(missingCorrelationId.headers.get("x-correlation-id"), /^corr_[A-Za-z0-9_-]{8,96}$/u);

    const bootstrapResponse = await fetch(`${baseUrl}/v1/bootstrap`, { headers: readHeaders });
    assert.equal(bootstrapResponse.status, 200);
    assert.equal((await bootstrapResponse.json()).api_version, "1.0.0");
    assert.equal(bootstrapResponse.headers.get("x-request-id"), readHeaders["X-Request-Id"]);
    assert.equal(bootstrapResponse.headers.get("x-trace-id"), readHeaders["X-Trace-Id"]);
    assert.equal(bootstrapResponse.headers.get("x-correlation-id"), readHeaders["X-Correlation-Id"]);

    const invalidBodyResponse = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: writeHeaders,
      body: "{}",
    });
    assert.equal(invalidBodyResponse.status, 400);
    assert.equal((await invalidBodyResponse.json()).error.code, "INVALID_REQUEST");

    const textBodyResponse = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: { ...writeHeaders, "Content-Type": "text/plain", "Idempotency-Key": "idem_text_00000001" },
      body: JSON.stringify(example("game-skill-build-create-request.json")),
    });
    assert.equal(textBodyResponse.status, 400);

    const requestBody = JSON.stringify(example("game-skill-build-create-request.json"));
    const acceptedResponse = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: writeHeaders,
      body: requestBody,
    });
    assert.equal(acceptedResponse.status, 202);
    const accepted = await acceptedResponse.json();
    assert.equal(accepted.status, "ACCEPTED");
    assert.equal(accepted.trace_id, writeHeaders["X-Trace-Id"]);
    assert.equal(acceptedResponse.headers.get("location"), `/v1/commands/${accepted.command_id}`);
    assert.equal(acceptedResponse.headers.get("idempotency-replayed"), "false");

    const retryHeaders = {
      ...writeHeaders,
      "X-Request-Id": "req_mock_retry_000001",
      "X-Trace-Id": "trace_mock_retry_000001",
      "X-Correlation-Id": "corr_mock_retry_000001",
    };
    const duplicateResponse = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: retryHeaders,
      body: requestBody,
    });
    assert.equal(duplicateResponse.status, 202);
    const replayed = await duplicateResponse.json();
    assert.equal(replayed.command_id, accepted.command_id);
    assert.equal(replayed.trace_id, accepted.trace_id,
      "receipt replay must retain the original command trace");
    assert.equal(duplicateResponse.headers.get("x-request-id"), retryHeaders["X-Request-Id"]);
    assert.equal(duplicateResponse.headers.get("x-trace-id"), retryHeaders["X-Trace-Id"]);
    assert.equal(duplicateResponse.headers.get("x-correlation-id"), retryHeaders["X-Correlation-Id"]);
    assert.equal(duplicateResponse.headers.get("idempotency-replayed"), "true");

    const conflictingBody = example("game-skill-build-create-request.json");
    conflictingBody.display_name = `${conflictingBody.display_name}-changed`;
    const conflictingResponse = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: writeHeaders,
      body: JSON.stringify(conflictingBody),
    });
    assert.equal(conflictingResponse.status, 409);
    assert.equal((await conflictingResponse.json()).error.code, "IDEMPOTENCY_KEY_REUSED");

    const pollHeaders = {
      ...readHeaders,
      "X-Request-Id": "req_mock_poll_0000001",
      "X-Trace-Id": "trace_mock_poll_0000001",
      "X-Correlation-Id": "corr_mock_poll_0000001",
    };
    const commandResponse = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, {
      headers: pollHeaders,
    });
    assert.equal(commandResponse.status, 200);
    const command = await commandResponse.json();
    assert.equal(commandResponse.headers.get("x-request-id"), pollHeaders["X-Request-Id"]);
    assert.equal(commandResponse.headers.get("x-trace-id"), pollHeaders["X-Trace-Id"]);
    assert.equal(commandResponse.headers.get("x-correlation-id"), pollHeaders["X-Correlation-Id"]);
    assert.equal(command.command_id, accepted.command_id);
    assert.equal(command.request_context.request_id, writeHeaders["X-Request-Id"]);
    assert.equal(command.request_context.trace_id, writeHeaders["X-Trace-Id"]);
    assert.equal(command.request_context.correlation_id, writeHeaders["X-Correlation-Id"]);
    assert.equal(command.request_context.actor.actor_id, "student_0001");
    assert.equal(command.command_type, "CREATE_SKILL_BUILD");
    assert.equal(command.status, "APPLIED");
    assert.equal(command.terminal, true);
    assert.equal(command.result.result_type, "RESOURCE_CREATED");
    assert.equal(command.result.resource_type, "SKILL_BUILD");
    const resourcePollHeaders = {
      ...readHeaders,
      "X-Request-Id": "req_mock_resource_0001",
      "X-Trace-Id": "trace_mock_resource_0001",
      "X-Correlation-Id": "corr_mock_resource_0001",
    };
    const createdBuildResponse = await fetch(`${baseUrl}${command.result.resource_url}`, {
      headers: resourcePollHeaders,
    });
    assert.equal(createdBuildResponse.status, 200);
    const createdBuild = await createdBuildResponse.json();
    assert.equal(createdBuild.build_id, command.result.resource_id);
    assert.equal(createdBuild.request_context.request_id, writeHeaders["X-Request-Id"]);
    assert.equal(createdBuild.request_context.trace_id, writeHeaders["X-Trace-Id"]);
    assert.equal(createdBuild.request_context.correlation_id, writeHeaders["X-Correlation-Id"]);
    assert.equal(createdBuildResponse.headers.get("x-correlation-id"), resourcePollHeaders["X-Correlation-Id"]);

    const missingCommand = await fetch(`${baseUrl}/v1/commands/cmd_does_not_exist`, { headers: readHeaders });
    assert.equal(missingCommand.status, 404);
    assert.equal((await missingCommand.json()).error.code, "NOT_FOUND");

    const snapshotResponse = await fetch(`${baseUrl}/v1/worlds/world_demo_001/snapshot`, { headers: readHeaders });
    const snapshot = await snapshotResponse.json();
    assert.equal(snapshotResponse.status, 200);
    assert.ok(snapshot.revision >= 0);
    assert.match(snapshotResponse.headers.get("etag"), /^"[a-f0-9]{64}"$/u);

    const wrongWorld = await fetch(`${baseUrl}/v1/worlds/world_other_999/snapshot`, { headers: readHeaders });
    assert.equal(wrongWorld.status, 404);
    assert.equal((await wrongWorld.json()).error.code, "NOT_FOUND");

    const missingCursor = await fetch(`${baseUrl}/v1/worlds/world_demo_001/events`, { headers: readHeaders });
    assert.equal(missingCursor.status, 400);
    const validAfter = example("game-world-event-page.json").from_sequence - 1;
    const eventPageResponse = await fetch(
      `${baseUrl}/v1/worlds/world_demo_001/events?after_sequence=${validAfter}`,
      { headers: readHeaders },
    );
    assert.equal(eventPageResponse.status, 200);
    const page = await eventPageResponse.json();
    assert.equal(eventPageResponse.headers.get("x-world-revision"), String(page.snapshot_revision));
    assert.deepEqual(page.events, []);
    assert.equal(page.next_after_sequence, validAfter);
    const exhaustedPageResponse = await fetch(
      `${baseUrl}/v1/worlds/world_demo_001/events?after_sequence=${page.next_after_sequence}`,
      { headers: readHeaders },
    );
    assert.equal(exhaustedPageResponse.status, 200);
    const exhaustedPage = await exhaustedPageResponse.json();
    assert.deepEqual(exhaustedPage.events, []);
    assert.equal(exhaustedPage.next_after_sequence, page.next_after_sequence);
    const backwardPage = await fetch(
      `${baseUrl}/v1/worlds/world_demo_001/events?after_sequence=999`,
      { headers: readHeaders },
    );
    assert.equal(backwardPage.status, 409);
    assert.equal((await backwardPage.json()).error.code, "EVENT_SEQUENCE_GAP");

    const evidenceId = example("game-evidence.json").evidence_ref.evidence_id;
    const evidenceResponse = await fetch(`${baseUrl}/v1/evidence/${evidenceId}`, { headers: readHeaders });
    assert.equal(evidenceResponse.status, 200);
    assert.match(evidenceResponse.headers.get("etag"), /^"[a-f0-9]{64}"$/u);

    const shortIdempotency = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "too-short" },
      body: requestBody,
    });
    assert.equal(shortIdempotency.status, 400);
  } finally {
    await close(server);
  }
});

test("mock rejects forged source hashes, duplicate paths and missing entrypoints before execution", async () => {
  let executedBuilds = 0;
  const server = createMockServer({
    now: () => FIXED_NOW,
    feishuSecret: FEISHU_SECRET,
    responseTransform(payload, metadata) {
      if (metadata.pathname === "/v1/skill-builds") executedBuilds += 1;
      return payload;
    },
  });
  const baseUrl = await listen(server);
  const cases = [
    {
      reason: "SOURCE_CONTENT_HASH_MISMATCH",
      mutate(request) {
        request.source_bundle.files[0].content_sha256 = "0".repeat(64);
      },
    },
    {
      reason: "DUPLICATE_SOURCE_PATH",
      mutate(request) {
        const duplicate = structuredClone(request.source_bundle.files[0]);
        duplicate.content = `${duplicate.content}// duplicate path\n`;
        duplicate.content_sha256 = utf8Sha256(duplicate.content);
        request.source_bundle.files.push(duplicate);
      },
    },
    {
      reason: "SOURCE_ENTRYPOINT_NOT_FOUND",
      mutate(request) {
        request.source_bundle.entrypoint = "src/missing.cpp";
      },
    },
  ];
  try {
    for (const [index, testCase] of cases.entries()) {
      const request = example("game-skill-build-create-request.json");
      testCase.mutate(request);
      const response = await fetch(`${baseUrl}/v1/skill-builds`, {
        method: "POST",
        headers: { ...writeHeaders, "Idempotency-Key": `idem_source_integrity_000${index}` },
        body: JSON.stringify(request),
      });
      assert.equal(response.status, 400);
      const failure = await response.json();
      assert.equal(failure.error.code, "INVALID_REQUEST");
      assert.equal(failure.error.details.reason, testCase.reason);
    }
    assert.equal(executedBuilds, 0, "invalid source bundles must not reach build execution");

    const valid = example("game-skill-build-create-request.json");
    valid.source_bundle.files[0].content += "// UTF-8: 芽芽\n";
    valid.source_bundle.files[0].content_sha256 = utf8Sha256(valid.source_bundle.files[0].content);
    const accepted = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_source_integrity_valid" },
      body: JSON.stringify(valid),
    });
    assert.equal(accepted.status, 202);
    assert.equal(executedBuilds, 1);
  } finally {
    await close(server);
  }
});

test("mock enforces advertised source count and aggregate UTF-8 byte limits below the HTTP limit", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  try {
    const tooMany = example("game-skill-build-create-request.json");
    tooMany.source_bundle.files = Array.from({ length: 33 }, (_, index) => {
      const content = `int source_${index} = ${index};\n`;
      return {
        path: `src/source_${index}.cpp`,
        content,
        content_sha256: utf8Sha256(content),
      };
    });
    tooMany.source_bundle.entrypoint = tooMany.source_bundle.files[0].path;
    const tooManyResponse = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_source_files_limit_01" },
      body: JSON.stringify(tooMany),
    });
    assert.equal(tooManyResponse.status, 400);
    assert.equal((await tooManyResponse.json()).error.code, "INVALID_REQUEST");

    const oversized = example("game-skill-build-create-request.json");
    const multibyteContent = "芽".repeat(174_763);
    oversized.source_bundle.files = [0, 1].map((index) => ({
      path: `src/multibyte_${index}.cpp`,
      content: multibyteContent,
      content_sha256: utf8Sha256(multibyteContent),
    }));
    oversized.source_bundle.entrypoint = oversized.source_bundle.files[0].path;
    const oversizedResponse = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_source_bytes_limit_01" },
      body: JSON.stringify(oversized),
    });
    assert.equal(oversizedResponse.status, 400);
    const oversizedFailure = await oversizedResponse.json();
    assert.equal(oversizedFailure.error.code, "INVALID_REQUEST");
    assert.equal(oversizedFailure.error.details.reason, "SOURCE_BUNDLE_BYTES_EXCEEDED");
    assert.ok(oversizedFailure.error.details.total_source_bytes > 1_048_576);
    assert.equal(oversizedFailure.error.details.max_source_bytes, 1_048_576);

    const exactLimit = example("game-skill-build-create-request.json");
    const exactContent = "x".repeat(1_048_576);
    exactLimit.source_bundle.files = [{
      path: "src/main.cpp",
      content: exactContent,
      content_sha256: utf8Sha256(exactContent),
    }];
    exactLimit.source_bundle.entrypoint = "src/main.cpp";
    const exactBody = JSON.stringify(exactLimit);
    assert.ok(Buffer.byteLength(exactBody, "utf8") > 1_048_576,
      "HTTP transport must allow JSON overhead above the source byte limit");
    const exactResponse = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_source_exact_limit_01" },
      body: exactBody,
    });
    assert.equal(exactResponse.status, 202);
  } finally {
    await close(server);
  }
});

test("mock sanitizes invalid correlation headers before returning contract errors", async () => {
  const unhandledRejections = [];
  const onUnhandledRejection = (reason) => unhandledRejections.push(reason);
  process.on("unhandledRejection", onUnhandledRejection);
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  try {
    const invalidRequestId = await fetch(`${baseUrl}/v1/bootstrap`, {
      headers: { ...readHeaders, "X-Request-Id": "bad" },
      signal: AbortSignal.timeout(2_000),
    });
    assert.equal(invalidRequestId.status, 400);
    const requestFailure = await invalidRequestId.json();
    assert.equal(requestFailure.error.code, "INVALID_REQUEST");
    assert.match(requestFailure.request_id, /^req_[A-Za-z0-9_-]{8,96}$/u);
    assert.notEqual(requestFailure.request_id, "bad");
    assert.equal(invalidRequestId.headers.get("x-request-id"), requestFailure.request_id);

    const invalidTraceId = await fetch(
      `${baseUrl}/integrations/feishu/v1/evidence/evidence_mock_00000001?purpose=TEACHER_SUPPORT`,
      {
        headers: {
          ...readHeaders,
          Authorization: "Bearer tenant_yaya:operator_0001",
          "X-Trace-Id": "bad",
        },
        signal: AbortSignal.timeout(2_000),
      },
    );
    assert.equal(invalidTraceId.status, 400);
    const traceFailure = await invalidTraceId.json();
    assert.equal(traceFailure.error.code, "INVALID_REQUEST");
    assert.match(traceFailure.trace_id, /^trace_[A-Za-z0-9_-]{8,96}$/u);
    assert.notEqual(traceFailure.trace_id, "bad");
    assert.equal(invalidTraceId.headers.get("x-trace-id"), traceFailure.trace_id);

    await new Promise((resolvePromise) => setImmediate(resolvePromise));
    assert.deepEqual(unhandledRejections, []);
  } finally {
    process.off("unhandledRejection", onUnhandledRejection);
    await close(server);
  }
});

test("mock RESOURCE_CREATED results point to retrievable materialized resources", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  try {
    const sessionBody = JSON.stringify(example("game-agent-session-create-request.json"));
    const acceptedResponse = await fetch(`${baseUrl}/v1/agent-sessions`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_session_00000001" },
      body: sessionBody,
    });
    assert.equal(acceptedResponse.status, 202);
    const accepted = await acceptedResponse.json();
    const commandResponse = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, { headers: readHeaders });
    assert.equal(commandResponse.status, 200);
    const command = await commandResponse.json();
    assert.equal(command.result.resource_type, "AGENT_SESSION");
    const resourceResponse = await fetch(`${baseUrl}${command.result.resource_url}`, { headers: readHeaders });
    assert.equal(resourceResponse.status, 200);
    assert.equal((await resourceResponse.json()).session_id, command.result.resource_id);
  } finally {
    await close(server);
  }
});

test("mock materializes independent resources and advances world commits monotonically", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  try {
    const createdBuilds = [];
    for (const [index, skillId] of [[1, "skill_water_001"], [2, "skill_other_002"]]) {
      const request = example("game-skill-build-create-request.json");
      request.skill_id = skillId;
      request.display_name = `build-${index}`;
      request.source_bundle.files[0].content += `// build ${index}\n`;
      request.source_bundle.files[0].content_sha256 = utf8Sha256(request.source_bundle.files[0].content);
      const acceptedResponse = await fetch(`${baseUrl}/v1/skill-builds`, {
        method: "POST",
        headers: { ...writeHeaders, "Idempotency-Key": `idem_build_state_00000${index}` },
        body: JSON.stringify(request),
      });
      assert.equal(acceptedResponse.status, 202);
      const accepted = await acceptedResponse.json();
      const command = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, { headers: readHeaders })
        .then((response) => response.json());
      const resource = await fetch(`${baseUrl}${command.result.resource_url}`, { headers: readHeaders })
        .then((response) => response.json());
      assert.equal(resource.skill_id, skillId);
      createdBuilds.push(resource.build_id);
    }
    assert.notEqual(createdBuilds[0], createdBuilds[1]);

    const sessions = [];
    for (const [index, learnerId, profileId] of [
      [1, "learner_0001", "agent_farmer_001"],
      [2, "learner_0002", "agent_farmer_002"],
    ]) {
      const request = example("game-agent-session-create-request.json");
      request.learner_id = learnerId;
      request.agent_profile_id = profileId;
      const acceptedResponse = await fetch(`${baseUrl}/v1/agent-sessions`, {
        method: "POST",
        headers: { ...writeHeaders, "Idempotency-Key": `idem_session_state_000${index}` },
        body: JSON.stringify(request),
      });
      assert.equal(acceptedResponse.status, 202);
      const accepted = await acceptedResponse.json();
      const command = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, { headers: readHeaders })
        .then((response) => response.json());
      const resource = await fetch(`${baseUrl}${command.result.resource_url}`, { headers: readHeaders })
        .then((response) => response.json());
      assert.equal(resource.learner_id, learnerId);
      assert.equal(resource.agent_profile_id, profileId);
      sessions.push(resource.session_id);
    }
    assert.notEqual(sessions[0], sessions[1]);

    const commitResults = [];
    for (const [index, expectedRevision, lastSequence] of [[1, 184, 731], [2, 185, 733]]) {
      const request = example("game-agent-turn-create-request.json");
      request.turn_id = `turn_state_0000000${index}`;
      request.expected_world_revision = expectedRevision;
      request.client_state.last_event_sequence = lastSequence;
      request.client_state.client_turn_sequence = index;
      const acceptedResponse = await fetch(`${baseUrl}/v1/agent-sessions/${sessions[0]}/turns`, {
        method: "POST",
        headers: { ...writeHeaders, "Idempotency-Key": `idem_turn_state_00000${index}` },
        body: JSON.stringify(request),
      });
      assert.equal(acceptedResponse.status, 202);
      const accepted = await acceptedResponse.json();
      const commandResponse = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, { headers: readHeaders });
      assert.equal(commandResponse.status, 200);
      commitResults.push((await commandResponse.json()).result);
    }
    assert.deepEqual(
      commitResults.map(({ previous_revision, world_revision, first_event_sequence, last_event_sequence }) => (
        [previous_revision, world_revision, first_event_sequence, last_event_sequence]
      )),
      [[184, 185, 732, 733], [185, 186, 734, 735]],
    );

    const staleTurn = example("game-agent-turn-create-request.json");
    staleTurn.turn_id = "turn_state_stale_01";
    staleTurn.expected_world_revision = 184;
    staleTurn.client_state.client_turn_sequence = 3;
    const staleResponse = await fetch(`${baseUrl}/v1/agent-sessions/${sessions[0]}/turns`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_turn_state_stale01" },
      body: JSON.stringify(staleTurn),
    });
    assert.equal(staleResponse.status, 409);
    assert.equal((await staleResponse.json()).error.code, "WORLD_REVISION_CONFLICT");

    const snapshotResponse = await fetch(`${baseUrl}/v1/worlds/world_demo_001/snapshot`, { headers: readHeaders });
    const snapshot = await snapshotResponse.json();
    assert.equal(snapshot.revision, 186);
    assert.equal(snapshot.last_event_sequence, 735);
    assert.equal(snapshotResponse.headers.get("x-world-revision"), "186");

    const firstPage = await fetch(
      `${baseUrl}/v1/worlds/world_demo_001/events?after_sequence=731&limit=1`,
      { headers: readHeaders },
    ).then((response) => response.json());
    assert.equal(firstPage.events.length, 1);
    assert.equal(firstPage.events[0].sequence, 732);
    assert.equal(firstPage.has_more, true);
    const secondPage = await fetch(
      `${baseUrl}/v1/worlds/world_demo_001/events?after_sequence=732&limit=500`,
      { headers: readHeaders },
    ).then((response) => response.json());
    assert.deepEqual(secondPage.events.map((event) => event.sequence), [733, 734, 735]);
    assert.equal(secondPage.has_more, false);
  } finally {
    await close(server);
  }
});

test("mock preserves a committed idempotency receipt when outbound serialization fails", async () => {
  let corruptFirstTurnResponse = true;
  const server = createMockServer({
    now: () => FIXED_NOW,
    feishuSecret: FEISHU_SECRET,
    responseTransform: (payload, meta) => {
      if (corruptFirstTurnResponse
        && meta.pathname === "/v1/agent-sessions/session_agent_001/turns") {
        corruptFirstTurnResponse = false;
        delete payload.command_id;
      }
      return payload;
    },
  });
  const baseUrl = await listen(server);
  const turnBody = JSON.stringify(example("game-agent-turn-create-request.json"));
  const headers = { ...writeHeaders, "Idempotency-Key": "idem_turn_serial_000001" };
  try {
    const first = await fetch(`${baseUrl}/v1/agent-sessions/session_agent_001/turns`, {
      method: "POST", headers, body: turnBody,
    });
    assert.equal(first.status, 503);
    const firstFailure = await first.json();
    assert.equal(firstFailure.error.code, "UNKNOWN_COMMIT_STATE");
    assert.equal(firstFailure.status, "UNKNOWN");
    assert.equal(firstFailure.error.stage, "WORLD_COMMIT");
    assert.equal(firstFailure.error.retryable, false);
    assert.equal(
      firstFailure.error.details.reason,
      "RESPONSE_DELIVERY_FAILED_AFTER_DURABLE_ACCEPT",
    );
    assert.equal(firstFailure.error.details.operation_was_durably_accepted, true);
    assert.equal(
      first.headers.get("location"),
      `/v1/commands/${firstFailure.command_id}`,
    );

    const reconciledBeforeReplay = await fetch(
      `${baseUrl}${first.headers.get("location")}`,
      { headers: readHeaders },
    );
    assert.equal(reconciledBeforeReplay.status, 200);
    const reconciledCommand = await reconciledBeforeReplay.json();
    assert.equal(reconciledCommand.command_id, firstFailure.command_id);
    assert.equal(reconciledCommand.result.previous_revision, 184);
    assert.equal(reconciledCommand.result.world_revision, 185);

    const retryHeaders = {
      ...headers,
      "X-Request-Id": "req_turn_serial_retry_01",
      "X-Trace-Id": "trace_turn_serial_retry_01",
      "X-Correlation-Id": "corr_turn_serial_retry_01",
    };
    const retry = await fetch(`${baseUrl}/v1/agent-sessions/session_agent_001/turns`, {
      method: "POST", headers: retryHeaders, body: turnBody,
    });
    assert.equal(retry.status, 202);
    assert.equal(retry.headers.get("idempotency-replayed"), "true");
    const accepted = await retry.json();
    assert.equal(accepted.command_id, firstFailure.command_id);
    const commandResponse = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, { headers: readHeaders });
    assert.equal(commandResponse.status, 200);
    const command = await commandResponse.json();
    assert.equal(command.result.previous_revision, 184);
    assert.equal(command.result.world_revision, 185);
    const snapshot = await fetch(`${baseUrl}/v1/worlds/world_demo_001/snapshot`, { headers: readHeaders })
      .then((response) => response.json());
    assert.equal(snapshot.revision, 185);
    assert.equal(snapshot.last_event_sequence, 733);
  } finally {
    await close(server);
  }
});

test("mock reconciles the canonical command when a schema-valid accepted receipt is relabeled", async (context) => {
  const cases = {
    "command and trace": (payload) => ({
      ...payload,
      command_id: "cmd_outbound_relabel_0001",
      trace_id: "trace_outbound_relabel_0001",
    }),
    trace: (payload) => ({ ...payload, trace_id: "trace_outbound_relabel_0001" }),
  };

  for (const [label, relabel] of Object.entries(cases)) {
    await context.test(label, async () => {
      const server = createMockServer({
        now: () => FIXED_NOW,
        feishuSecret: FEISHU_SECRET,
        logger: () => {},
        responseTransform(payload, metadata) {
          return metadata.schema === "acceptedGameJob" ? relabel(payload) : payload;
        },
      });
      const baseUrl = await listen(server);
      const body = JSON.stringify(example("game-skill-build-create-request.json"));
      try {
        const failedDelivery = await fetch(`${baseUrl}/v1/skill-builds`, {
          method: "POST",
          headers: writeHeaders,
          body,
        });
        assert.equal(failedDelivery.status, 503);
        const failure = await failedDelivery.json();
        assert.equal(failure.error.code, "UNKNOWN_COMMIT_STATE");
        assert.equal(failure.status, "UNKNOWN");
        assert.notEqual(failure.command_id, "cmd_outbound_relabel_0001");
        assert.equal(
          failedDelivery.headers.get("location"),
          `/v1/commands/${failure.command_id}`,
        );

        const canonicalCommand = await fetch(`${baseUrl}${failedDelivery.headers.get("location")}`, {
          headers: readHeaders,
        });
        assert.equal(canonicalCommand.status, 200);
        const command = await canonicalCommand.json();
        assert.equal(command.command_id, failure.command_id);
        assert.equal(command.request_context.trace_id, writeHeaders["X-Trace-Id"]);

        const replay = await fetch(`${baseUrl}/v1/skill-builds`, {
          method: "POST",
          headers: {
            ...writeHeaders,
            "X-Request-Id": "req_relabel_retry_0001",
            "X-Trace-Id": "trace_relabel_retry_0001",
            "X-Correlation-Id": "corr_relabel_retry_0001",
          },
          body,
        });
        assert.equal(replay.status, 202);
        assert.equal(replay.headers.get("idempotency-replayed"), "true");
        const accepted = await replay.json();
        assert.equal(accepted.command_id, failure.command_id);
        assert.equal(accepted.trace_id, writeHeaders["X-Trace-Id"]);
        assert.equal(replay.headers.get("location"), `/v1/commands/${failure.command_id}`);
      } finally {
        await close(server);
      }
    });
  }
});

test("mock rejects new idempotency keys at capacity without evicting committed receipts", async () => {
  let executedResponses = 0;
  const server = createMockServer({
    now: () => FIXED_NOW,
    feishuSecret: FEISHU_SECRET,
    idempotencyCapacity: 1,
    responseTransform(payload, metadata) {
      if (metadata.pathname === "/v1/skill-builds") executedResponses += 1;
      return payload;
    },
  });
  const baseUrl = await listen(server);
  const body = JSON.stringify(example("game-skill-build-create-request.json"));
  const firstHeaders = { ...writeHeaders, "Idempotency-Key": "idem_capacity_first_001" };
  try {
    const first = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST", headers: firstHeaders, body,
    });
    assert.equal(first.status, 202);
    const committedReceipt = await first.json();

    const overflow = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_capacity_second_01" },
      body,
    });
    assert.equal(overflow.status, 429);
    assert.match(overflow.headers.get("retry-after"), /^[1-9][0-9]*$/u);
    const capacityFailure = await overflow.json();
    assert.equal(capacityFailure.error.code, "RATE_LIMITED");
    assert.equal(capacityFailure.error.category, "RATE_LIMIT");
    assert.equal(capacityFailure.error.stage, "IDEMPOTENCY");
    assert.deepEqual(capacityFailure.error.details, {
      reason: "IDEMPOTENCY_CAPACITY_EXHAUSTED",
      capacity: 1,
    });

    const replay = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST", headers: firstHeaders, body,
    });
    assert.equal(replay.status, 202);
    assert.equal(replay.headers.get("idempotency-replayed"), "true");
    assert.deepEqual(await replay.json(), committedReceipt);
    assert.equal(executedResponses, 1, "neither the rejected key nor replay may execute the route again");
  } finally {
    await close(server);
  }
});

test("mock rejects unknown worlds, stale cursors and uncertified skill bindings without committing", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  try {
    const unknownWorld = example("game-agent-session-create-request.json");
    unknownWorld.world_id = "world_other_002";
    const unknownWorldResponse = await fetch(`${baseUrl}/v1/agent-sessions`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_unknown_world_0001" },
      body: JSON.stringify(unknownWorld),
    });
    assert.equal(unknownWorldResponse.status, 404);
    assert.equal((await unknownWorldResponse.json()).error.code, "NOT_FOUND");

    const staleCursor = example("game-agent-turn-create-request.json");
    staleCursor.client_state.last_event_sequence = 999;
    const staleCursorResponse = await fetch(`${baseUrl}/v1/agent-sessions/session_agent_001/turns`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_future_cursor_0001" },
      body: JSON.stringify(staleCursor),
    });
    assert.equal(staleCursorResponse.status, 409);
    assert.equal((await staleCursorResponse.json()).error.code, "EVENT_SEQUENCE_GAP");

    const uncertified = example("game-agent-turn-create-request.json");
    uncertified.skill_bindings[0].skill_version_id = "skillver_missing_999";
    uncertified.skill_bindings[0].certification_id = "cert_missing_999";
    uncertified.skill_bindings[0].artifact_sha256 = "f".repeat(64);
    const uncertifiedResponse = await fetch(`${baseUrl}/v1/agent-sessions/session_agent_001/turns`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_uncertified_00001" },
      body: JSON.stringify(uncertified),
    });
    assert.equal(uncertifiedResponse.status, 422);
    assert.equal((await uncertifiedResponse.json()).error.code, "SKILL_NOT_CERTIFIED");

    const snapshot = await fetch(`${baseUrl}/v1/worlds/world_demo_001/snapshot`, { headers: readHeaders })
      .then((response) => response.json());
    assert.equal(snapshot.revision, 184);
    assert.equal(snapshot.last_event_sequence, 731);
  } finally {
    await close(server);
  }
});

test("mock scopes resources to tenants and never relabels leaked resources", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  const alphaRead = {
    ...readHeaders,
    Authorization: "Bearer tenant_alpha:student_0001",
    "X-Request-Id": "req_tenant_alpha_001",
    "X-Trace-Id": "trace_tenant_alpha_001",
  };
  const betaRead = {
    ...readHeaders,
    Authorization: "Bearer tenant_beta:student_0001",
    "X-Request-Id": "req_tenant_beta_0001",
    "X-Trace-Id": "trace_tenant_beta_0001",
    "X-Correlation-Id": "corr_tenant_beta_0001",
  };
  const alphaOtherActorRead = {
    ...alphaRead,
    Authorization: "Bearer tenant_alpha:student_0002",
    "X-Request-Id": "req_tenant_alpha_other_01",
    "X-Trace-Id": "trace_tenant_alpha_other_01",
    "X-Correlation-Id": "corr_tenant_alpha_other_01",
  };
  try {
    const buildBody = JSON.stringify(example("game-skill-build-create-request.json"));
    const created = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: {
        ...alphaRead,
        "Content-Type": "application/json",
        "Idempotency-Key": "idem_tenant_alpha_0001",
      },
      body: buildBody,
    });
    assert.equal(created.status, 202);
    const accepted = await created.json();
    const command = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, { headers: alphaRead })
      .then((response) => response.json());

    const sameKeyOtherActor = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: {
        ...alphaOtherActorRead,
        "Content-Type": "application/json",
        "Idempotency-Key": "idem_tenant_alpha_0001",
      },
      body: buildBody,
    });
    assert.equal(sameKeyOtherActor.status, 202);
    assert.equal(sameKeyOtherActor.headers.get("idempotency-replayed"), "false");
    const otherActorAccepted = await sameKeyOtherActor.json();
    assert.notEqual(otherActorAccepted.command_id, accepted.command_id);
    const ownCommand = await fetch(`${baseUrl}/v1/commands/${otherActorAccepted.command_id}`, {
      headers: alphaOtherActorRead,
    });
    assert.equal(ownCommand.status, 200);

    const otherActorCommand = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, {
      headers: alphaOtherActorRead,
    });
    assert.equal(otherActorCommand.status, 404);
    const leakedCommand = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, { headers: betaRead });
    assert.equal(leakedCommand.status, 404);
    const leakedBuild = await fetch(`${baseUrl}${command.result.resource_url}`, { headers: betaRead });
    assert.equal(leakedBuild.status, 404);

    const contentBody = JSON.stringify(example("feishu-content-release-request.json"));
    const releaseResponse = await fetch(`${baseUrl}/integrations/feishu/v1/content-releases`, {
      method: "POST",
      headers: feishuHeaders(contentBody, "nonce_tenant_release", {
        idempotencyKey: "idem_tenant_release_001",
      }),
      body: contentBody,
    });
    assert.equal(releaseResponse.status, 202);
    const release = await releaseResponse.json();
    const crossTenantRelease = await fetch(
      `${baseUrl}/integrations/feishu/v1/content-releases/${release.release_id}`,
      {
        headers: {
          Authorization: "Bearer tenant_other:feishu_teacher_001",
          "X-Request-Id": "req_tenant_other_001",
          "X-Trace-Id": "trace_tenant_other_001",
          "X-Schema-Version": "1.0.0",
        },
      },
    );
    assert.equal(crossTenantRelease.status, 404);
  } finally {
    await close(server);
  }
});

test("mock skill activation enforces registry revision and materializes a readable resource", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  const path = "/v1/skill-versions/skillver_water_001/activations";
  try {
    const stale = example("game-skill-activation-request.json");
    stale.expected_registry_revision = 999;
    const staleResponse = await fetch(`${baseUrl}${path}`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_activation_stale_01" },
      body: JSON.stringify(stale),
    });
    assert.equal(staleResponse.status, 409);
    assert.equal((await staleResponse.json()).error.code, "SKILL_VERSION_MISMATCH");

    const unknownProfile = example("game-skill-activation-request.json");
    unknownProfile.activation_scope.agent_profile_id = "agent_missing_999";
    const unknownProfileResponse = await fetch(`${baseUrl}${path}`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_activation_profile_1" },
      body: JSON.stringify(unknownProfile),
    });
    assert.equal(unknownProfileResponse.status, 404);

    const activationBody = JSON.stringify(example("game-skill-activation-request.json"));
    const activationResponse = await fetch(`${baseUrl}${path}`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_activation_valid_001" },
      body: activationBody,
    });
    assert.equal(activationResponse.status, 202);
    const accepted = await activationResponse.json();
    const command = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, { headers: readHeaders })
      .then((response) => response.json());
    assert.equal(command.result.resource_type, "SKILL_ACTIVATION");
    const materializedResponse = await fetch(`${baseUrl}${command.result.resource_url}`, { headers: readHeaders });
    assert.equal(materializedResponse.status, 200);
    const activation = await materializedResponse.json();
    assert.equal(activation.activation_id, command.result.resource_id);
    assert.equal(activation.skill_version_id, "skillver_water_001");
    assert.equal(activation.previous_registry_revision, 17);
    assert.equal(activation.registry_revision, 18);
  } finally {
    await close(server);
  }
});

test("mock deduplicates client event IDs across idempotency keys", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  const body = JSON.stringify(example("game-client-event-batch-request.json"));
  try {
    const results = [];
    for (const [index, key] of [[1, "idem_client_events_first"], [2, "idem_client_events_again"]]) {
      const response = await fetch(`${baseUrl}/v1/client-events:batch`, {
        method: "POST",
        headers: { ...writeHeaders, "Idempotency-Key": key },
        body,
      });
      assert.equal(response.status, 202, `submission ${index}`);
      const accepted = await response.json();
      const command = await fetch(`${baseUrl}/v1/commands/${accepted.command_id}`, { headers: readHeaders })
        .then((commandResponse) => commandResponse.json());
      results.push(command.result);
    }
    assert.deepEqual(
      results.map(({ accepted_count, duplicate_count, rejected_count }) => ({
        accepted_count, duplicate_count, rejected_count,
      })),
      [
        { accepted_count: 2, duplicate_count: 0, rejected_count: 0 },
        { accepted_count: 0, duplicate_count: 2, rejected_count: 0 },
      ],
    );
  } finally {
    await close(server);
  }
});

test("mock classifies its own invalid response as 500 instead of blaming the client", async () => {
  const server = createMockServer({
    now: () => FIXED_NOW,
    feishuSecret: FEISHU_SECRET,
    responseTransform(payload, metadata) {
      if (metadata.pathname === "/v1/bootstrap") delete payload.api_version;
      return payload;
    },
  });
  const baseUrl = await listen(server);
  try {
    const response = await fetch(`${baseUrl}/v1/bootstrap`, { headers: readHeaders });
    assert.equal(response.status, 500);
    const failure = await response.json();
    assert.equal(failure.error.code, "INTERNAL_ERROR");
    assert.equal(failure.status, "FAILED");
  } finally {
    await close(server);
  }
});

test("mock logs unknown exception stacks and returns a sanitized internal error", async () => {
  const logs = [];
  const server = createMockServer({
    now: () => FIXED_NOW,
    feishuSecret: FEISHU_SECRET,
    logger: { error: (entry) => logs.push(entry) },
    responseTransform() {
      throw new Error("super-secret-stack-marker");
    },
  });
  const baseUrl = await listen(server);
  try {
    const response = await fetch(`${baseUrl}/v1/bootstrap`, { headers: readHeaders });
    assert.equal(response.status, 500);
    const failure = await response.json();
    assert.equal(failure.error.code, "INTERNAL_ERROR");
    assert.deepEqual(failure.error.details, { incident_id: `incident_${FIXED_NOW}` });
    assert.doesNotMatch(JSON.stringify(failure), /super-secret-stack-marker|\bstack\b|Error:/u);

    assert.equal(logs.length, 1);
    assert.equal(logs[0].event, "mock_server_internal_error");
    assert.equal(logs[0].incident_id, failure.error.details.incident_id);
    assert.equal(logs[0].request_id, readHeaders["X-Request-Id"]);
    assert.equal(logs[0].trace_id, readHeaders["X-Trace-Id"]);
    assert.match(logs[0].stack, /Error: super-secret-stack-marker/u);
  } finally {
    await close(server);
  }
});

test("mock exposes reconciliation when an accepted job cannot be delivered safely", async () => {
  for (const transform of [
    (payload) => { delete payload.command_id; return payload; },
    (payload) => ({ ...payload, status: "RUNNING" }),
  ]) {
    const server = createMockServer({
      now: () => FIXED_NOW,
      feishuSecret: FEISHU_SECRET,
      responseTransform(payload, metadata) {
        return metadata.pathname === "/v1/skill-builds" ? transform(payload) : payload;
      },
    });
    const baseUrl = await listen(server);
    try {
      const body = JSON.stringify(example("game-skill-build-create-request.json"));
      const response = await fetch(`${baseUrl}/v1/skill-builds`, {
        method: "POST",
        headers: writeHeaders,
        body,
      });
      assert.equal(response.status, 503);
      const failure = await response.json();
      assert.equal(failure.error.code, "UNKNOWN_COMMIT_STATE");
      assert.equal(failure.status, "UNKNOWN");
      assert.equal(failure.error.stage, "WORLD_COMMIT");
      assert.equal(failure.error.retryable, false);
      assert.equal(failure.error.details.operation_was_durably_accepted, true);
      assert.equal(
        response.headers.get("location"),
        `/v1/commands/${failure.command_id}`,
      );
      const command = await fetch(`${baseUrl}${response.headers.get("location")}`, {
        headers: readHeaders,
      });
      assert.equal(command.status, 200);
    } finally {
      await close(server);
    }
  }
});

test("mock rejects outbound cross-field invariant corruption as 500", async () => {
  const server = createMockServer({
    now: () => FIXED_NOW,
    feishuSecret: FEISHU_SECRET,
    responseTransform: (payload, meta) => {
      if (meta.schema === "gameEvidence" && payload?.payload?.evidence_kind === "WORLD_COMMIT") {
        payload.payload.world_revision = payload.payload.previous_revision + 99;
      }
      return payload;
    },
  });
  const baseUrl = await listen(server);
  const evidenceId = example("game-evidence.json").evidence_ref.evidence_id;
  try {
    const response = await fetch(`${baseUrl}/v1/evidence/${evidenceId}`, { headers: readHeaders });
    assert.equal(response.status, 500);
    const error = await response.json();
    assert.equal(error.error.code, "INTERNAL_ERROR");
    assert.equal(error.error.details.reason, "RESPONSE_SEMANTIC_INVARIANT");
    assert.equal(error.error.details.schema, "gameEvidence");
  } finally {
    await close(server);
  }
});

test("mock returns 413 for oversized bodies instead of hiding them as validation errors", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  try {
    const response = await fetch(`${baseUrl}/v1/skill-builds`, {
      method: "POST",
      headers: { ...writeHeaders, "Idempotency-Key": "idem_large_0000001" },
      body: "x".repeat(8 * 1024 * 1024 + 1),
    });
    assert.equal(response.status, 413);
    const failure = await response.json();
    assert.equal(failure.error.code, "PAYLOAD_TOO_LARGE");
    assert.deepEqual(failure.error.details, {
      limit_scope: "HTTP_BODY",
      limit_bytes: 8 * 1024 * 1024,
    });
  } finally {
    await close(server);
  }
});

test("mock Feishu boundary verifies bytes, time, replay, duplicate, quarantine and challenge", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  const webhookUrl = `${baseUrl}/integrations/feishu/v1/webhooks`;
  const event = example("feishu-webhook-event.json");
  const body = JSON.stringify(event);
  try {
    const tampered = await fetch(webhookUrl, {
      method: "POST",
      headers: feishuHeaders(body, "nonce_tamper_0001"),
      body: `${body} `,
    });
    assert.equal(tampered.status, 401);
    assert.equal((await tampered.json()).error.code, "FEISHU_SIGNATURE_INVALID");

    const staleTimestamp = String(Number(FIXED_TIMESTAMP) - 301);
    const stale = await fetch(webhookUrl, {
      method: "POST",
      headers: feishuHeaders(body, "nonce_stale_0001", { timestamp: staleTimestamp }),
      body,
    });
    assert.equal(stale.status, 401);

    const nonCanonicalTimestamp = await fetch(webhookUrl, {
      method: "POST",
      headers: feishuHeaders(body, "nonce_timestamp_format_0001", {
        timestamp: Number(FIXED_TIMESTAMP).toExponential(),
        idempotencyKey: "idem_timestamp_format_0001",
      }),
      body,
    });
    assert.equal(nonCanonicalTimestamp.status, 401);
    const timestampFailure = await nonCanonicalTimestamp.json();
    assert.equal(timestampFailure.error.code, "FEISHU_SIGNATURE_INVALID");
    assert.equal(timestampFailure.error.details.reason, "TIMESTAMP_INVALID_FORMAT");

    const oversizedNonce = "n".repeat(257);
    const nonCanonicalNonce = await fetch(webhookUrl, {
      method: "POST",
      headers: feishuHeaders(body, oversizedNonce, { idempotencyKey: "idem_nonce_length_000001" }),
      body,
    });
    assert.equal(nonCanonicalNonce.status, 401);
    const nonceFailure = await nonCanonicalNonce.json();
    assert.equal(nonceFailure.error.code, "FEISHU_SIGNATURE_INVALID");
    assert.equal(nonceFailure.error.details.reason, "NONCE_INVALID_LENGTH");

    const accepted = await fetch(webhookUrl, {
      method: "POST",
      headers: feishuHeaders(body, "nonce_accept_0001"),
      body,
    });
    assert.equal(accepted.status, 200);
    const receipt = await accepted.json();
    assert.equal(receipt.disposition, "ACCEPTED");

    const conflictingIdempotencyEvent = structuredClone(event);
    conflictingIdempotencyEvent.header.event_id = "evt_feishu_conflict_0001";
    const conflictingIdempotencyBody = JSON.stringify(conflictingIdempotencyEvent);
    const conflictingIdempotency = await fetch(webhookUrl, {
      method: "POST",
      headers: feishuHeaders(conflictingIdempotencyBody, "nonce_conflict_0001", {
        idempotencyKey: "idem_nonce_accept_0001",
      }),
      body: conflictingIdempotencyBody,
    });
    assert.equal(conflictingIdempotency.status, 409);
    assert.equal((await conflictingIdempotency.json()).error.code, "IDEMPOTENCY_KEY_REUSED");

    const duplicate = await fetch(webhookUrl, {
      method: "POST",
      headers: feishuHeaders(body, "nonce_duplicate_0001", { idempotencyKey: "idem_duplicate_0001" }),
      body,
    });
    assert.equal(duplicate.status, 200);
    assert.equal((await duplicate.json()).disposition, "DUPLICATE");

    const unknownEvent = structuredClone(event);
    unknownEvent.header.event_id = "evt_feishu_unknown_0001";
    unknownEvent.header.event_type = "totally.unsupported";
    const unknownBody = JSON.stringify(unknownEvent);
    const quarantined = await fetch(webhookUrl, {
      method: "POST",
      headers: feishuHeaders(unknownBody, "nonce_unknown_0001", { idempotencyKey: "idem_unknown_0001" }),
      body: unknownBody,
    });
    assert.equal(quarantined.status, 200);
    const quarantineReceipt = await quarantined.json();
    assert.equal(quarantineReceipt.disposition, "QUARANTINED_UNSUPPORTED");
    assert.match(quarantineReceipt.quarantine_reason, /totally\.unsupported/u);

    const replayEvent = structuredClone(event);
    replayEvent.header.event_id = "evt_feishu_replay_0001";
    const replayBody = JSON.stringify(replayEvent);
    const replay = await fetch(webhookUrl, {
      method: "POST",
      headers: feishuHeaders(replayBody, "nonce_accept_0001", { idempotencyKey: "idem_replay_0001" }),
      body: replayBody,
    });
    assert.equal(replay.status, 409);
    assert.equal((await replay.json()).error.code, "FEISHU_REPLAY_DETECTED");

    const challengeBody = JSON.stringify({
      type: "url_verification",
      challenge: "challenge_123",
      token: "verification-token",
    });
    const challenge = await fetch(webhookUrl, {
      method: "POST",
      headers: feishuHeaders(challengeBody, "nonce_challenge_0001", { idempotencyKey: "idem_challenge_0001" }),
      body: challengeBody,
    });
    assert.equal(challenge.status, 200);
    assert.deepEqual(await challenge.json(), { challenge: "challenge_123" });

    const forbiddenWorldRoute = await fetch(`${baseUrl}/integrations/feishu/v1/worlds/world_1/apply`, {
      method: "POST",
      headers: {
        ...writeHeaders,
        Authorization: "Bearer tenant_yaya:operator_0001",
        "Idempotency-Key": "idem_feishu_world_1",
      },
      body: "{}",
    });
    assert.equal(forbiddenWorldRoute.status, 404);
    assert.equal((await forbiddenWorldRoute.json()).error.code, "NOT_FOUND");
  } finally {
    await close(server);
  }
});

test("mock Feishu writes materialize isolated pollable resources", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  try {
    const contentBody = JSON.stringify(example("feishu-content-release-request.json"));
    const contentResponse = await fetch(`${baseUrl}/integrations/feishu/v1/content-releases`, {
      method: "POST",
      headers: feishuHeaders(contentBody, "nonce_release_0001", { idempotencyKey: "idem_release_00000001" }),
      body: contentBody,
    });
    assert.equal(contentResponse.status, 202);
    assert.equal(contentResponse.headers.get("retry-after"), "1");
    assert.equal(contentResponse.headers.get("idempotency-replayed"), "false");
    const contentReceipt = await contentResponse.json();
    assert.equal(contentReceipt.candidate_revision, 1);
    assert.equal(
      contentResponse.headers.get("location"),
      `/integrations/feishu/v1/content-releases/${contentReceipt.release_id}`,
    );

    const replayRequestId = "req_feishu_retry_0001";
    const replayTraceId = "trace_feishu_retry_0001";
    const replayContentResponse = await fetch(`${baseUrl}/integrations/feishu/v1/content-releases`, {
      method: "POST",
      headers: feishuHeaders(contentBody, "nonce_release_retry", {
        idempotencyKey: "idem_release_00000001",
        requestId: replayRequestId,
        traceId: replayTraceId,
      }),
      body: contentBody,
    });
    assert.equal(replayContentResponse.status, 202);
    assert.equal(replayContentResponse.headers.get("idempotency-replayed"), "true");
    assert.equal(replayContentResponse.headers.get("x-request-id"), replayRequestId);
    assert.equal(replayContentResponse.headers.get("x-trace-id"), replayTraceId);
    assert.deepEqual(await replayContentResponse.json(), contentReceipt);

    const secondContentResponse = await fetch(`${baseUrl}/integrations/feishu/v1/content-releases`, {
      method: "POST",
      headers: feishuHeaders(contentBody, "nonce_release_0002", { idempotencyKey: "idem_release_00000002" }),
      body: contentBody,
    });
    assert.equal(secondContentResponse.status, 202);
    const secondContentReceipt = await secondContentResponse.json();
    assert.notEqual(secondContentReceipt.release_id, contentReceipt.release_id);

    const feishuReadHeaders = {
      Authorization: "Bearer tenant_yaya:feishu_teacher_001",
      "X-Request-Id": "req_feishu_read_0001",
      "X-Trace-Id": "trace_feishu_read_0001",
      "X-Schema-Version": "1.0.0",
    };
    const contentStatusResponse = await fetch(
      `${baseUrl}/integrations/feishu/v1/content-releases/${contentReceipt.release_id}`,
      { headers: feishuReadHeaders },
    );
    assert.equal(contentStatusResponse.status, 200);
    const contentStatus = await contentStatusResponse.json();
    assert.equal(contentStatus.release_id, contentReceipt.release_id);
    assert.equal(contentStatus.candidate_id, contentReceipt.candidate_id);
    assert.equal(contentStatus.candidate_revision, contentReceipt.candidate_revision);
    assert.equal(contentStatus.validation_job.job_id, contentReceipt.validation_job.job_id);
    assert.equal(contentStatus.trace_id, contentReceipt.trace_id);

    const staleApproval = example("feishu-approval-decision-request.json");
    staleApproval.release_id = contentReceipt.release_id;
    staleApproval.candidate_id = contentReceipt.candidate_id;
    staleApproval.expected_candidate_revision = 999;
    const staleApprovalBody = JSON.stringify(staleApproval);
    const staleApprovalResponse = await fetch(`${baseUrl}/integrations/feishu/v1/approval-decisions`, {
      method: "POST",
      headers: feishuHeaders(staleApprovalBody, "nonce_approval_stale", {
        idempotencyKey: "idem_approval_stale_0001",
      }),
      body: staleApprovalBody,
    });
    assert.equal(staleApprovalResponse.status, 409);
    const staleFailure = await staleApprovalResponse.json();
    assert.equal(staleFailure.error.code, "CONTENT_VERSION_MISMATCH");
    assert.equal(staleFailure.error.details.expected_candidate_revision, 999);
    assert.equal(staleFailure.error.details.current_candidate_revision, 1);
    const afterStale = await fetch(
      `${baseUrl}/integrations/feishu/v1/content-releases/${contentReceipt.release_id}`,
      { headers: feishuReadHeaders },
    ).then((response) => response.json());
    assert.equal(afterStale.candidate_revision, 1, "stale approval must not mutate candidate state");

    const competingApprovals = [1, 2].map((index) => {
      const approval = example("feishu-approval-decision-request.json");
      approval.approval_instance_id = `approval_instance_race_000${index}`;
      approval.release_id = contentReceipt.release_id;
      approval.candidate_id = contentReceipt.candidate_id;
      approval.expected_candidate_revision = contentReceipt.candidate_revision;
      return { approval, body: JSON.stringify(approval), index };
    });
    const competingResults = await Promise.all(competingApprovals.map(async (attempt) => {
      const response = await fetch(`${baseUrl}/integrations/feishu/v1/approval-decisions`, {
        method: "POST",
        headers: feishuHeaders(attempt.body, `nonce_approval_race_${attempt.index}`, {
          idempotencyKey: `idem_approval_race_0000${attempt.index}`,
        }),
        body: attempt.body,
      });
      return { ...attempt, status: response.status, responseBody: await response.json() };
    }));
    assert.deepEqual(competingResults.map((result) => result.status).sort(), [200, 409]);
    const winningAttempt = competingResults.find((result) => result.status === 200);
    const losingAttempt = competingResults.find((result) => result.status === 409);
    const approvalReceipt = winningAttempt.responseBody;
    const raceFailure = losingAttempt.responseBody;
    assert.equal(raceFailure.error.code, "CONTENT_VERSION_MISMATCH");
    assert.equal(raceFailure.error.details.expected_candidate_revision, 1);
    assert.equal(raceFailure.error.details.current_candidate_revision, 2);
    assert.equal(approvalReceipt.release_id, contentReceipt.release_id);
    assert.equal(approvalReceipt.candidate_id, contentReceipt.candidate_id);
    assert.equal(approvalReceipt.trace_id, winningAttempt.approval.context.trace_id);
    assert.equal(approvalReceipt.candidate_revision, 2);

    const immutableReplay = await fetch(`${baseUrl}/integrations/feishu/v1/approval-decisions`, {
      method: "POST",
      headers: feishuHeaders(winningAttempt.body, "nonce_approval_immutable_replay", {
        idempotencyKey: "idem_approval_immutable_replay",
      }),
      body: winningAttempt.body,
    });
    assert.equal(immutableReplay.status, 200);
    assert.deepEqual(await immutableReplay.json(), approvalReceipt,
      "the same approval instance decision must return its original receipt");

    const conflictingDecision = structuredClone(winningAttempt.approval);
    conflictingDecision.decision = "REJECT";
    const conflictingDecisionBody = JSON.stringify(conflictingDecision);
    const conflictKey = "idem_approval_immutable_conflict";
    const immutableConflict = await fetch(`${baseUrl}/integrations/feishu/v1/approval-decisions`, {
      method: "POST",
      headers: feishuHeaders(conflictingDecisionBody, "nonce_approval_immutable_conflict", {
        idempotencyKey: conflictKey,
      }),
      body: conflictingDecisionBody,
    });
    assert.equal(immutableConflict.status, 409);
    const immutableFailure = await immutableConflict.json();
    assert.equal(immutableFailure.error.code, "CONTENT_VERSION_MISMATCH");
    assert.equal(immutableFailure.error.details.reason, "APPROVAL_INSTANCE_IMMUTABLE");
    assert.ok(immutableFailure.error.details.conflicting_fields.includes("decision"));
    const recoveredReplay = await fetch(`${baseUrl}/integrations/feishu/v1/approval-decisions`, {
      method: "POST",
      headers: feishuHeaders(winningAttempt.body, "nonce_approval_conflict_recovery", {
        idempotencyKey: conflictKey,
      }),
      body: winningAttempt.body,
    });
    assert.equal(recoveredReplay.status, 200,
      "a rejected immutable conflict must not poison its idempotency key");
    assert.deepEqual(await recoveredReplay.json(), approvalReceipt);

    const conflictingActor = structuredClone(winningAttempt.approval);
    conflictingActor.context.request_id = "req_feishu_actor_conflict_01";
    conflictingActor.context.trace_id = "trace_feishu_actor_conflict_01";
    conflictingActor.context.actor.actor_id = "feishu_operator_002";
    const conflictingActorBody = JSON.stringify(conflictingActor);
    const actorConflict = await fetch(`${baseUrl}/integrations/feishu/v1/approval-decisions`, {
      method: "POST",
      headers: feishuHeaders(conflictingActorBody, "nonce_approval_actor_conflict", {
        idempotencyKey: "idem_approval_actor_conflict_01",
      }),
      body: conflictingActorBody,
    });
    assert.equal(actorConflict.status, 409);
    assert.ok((await actorConflict.json()).error.details.conflicting_fields.includes("actor"));

    const conflictingCandidate = structuredClone(winningAttempt.approval);
    conflictingCandidate.release_id = secondContentReceipt.release_id;
    conflictingCandidate.candidate_id = secondContentReceipt.candidate_id;
    const conflictingCandidateBody = JSON.stringify(conflictingCandidate);
    const candidateConflict = await fetch(`${baseUrl}/integrations/feishu/v1/approval-decisions`, {
      method: "POST",
      headers: feishuHeaders(conflictingCandidateBody, "nonce_approval_candidate_conflict", {
        idempotencyKey: "idem_approval_candidate_conflict",
      }),
      body: conflictingCandidateBody,
    });
    assert.equal(candidateConflict.status, 409);
    assert.equal((await candidateConflict.json()).error.details.reason, "APPROVAL_INSTANCE_IMMUTABLE");

    const afterApproval = await fetch(
      `${baseUrl}/integrations/feishu/v1/content-releases/${contentReceipt.release_id}`,
      { headers: feishuReadHeaders },
    ).then((response) => response.json());
    assert.equal(afterApproval.candidate_revision, approvalReceipt.candidate_revision);

    const rejectingApproval = example("feishu-approval-decision-request.json");
    rejectingApproval.approval_instance_id = "approval_instance_terminal_01";
    rejectingApproval.release_id = secondContentReceipt.release_id;
    rejectingApproval.candidate_id = secondContentReceipt.candidate_id;
    rejectingApproval.expected_candidate_revision = secondContentReceipt.candidate_revision;
    rejectingApproval.decision = "REJECT";
    const rejectingBody = JSON.stringify(rejectingApproval);
    const rejectingResponse = await fetch(`${baseUrl}/integrations/feishu/v1/approval-decisions`, {
      method: "POST",
      headers: feishuHeaders(rejectingBody, "nonce_approval_terminal", {
        idempotencyKey: "idem_approval_terminal_0001",
      }),
      body: rejectingBody,
    });
    assert.equal(rejectingResponse.status, 200);
    const rejectingReceipt = await rejectingResponse.json();
    assert.equal(rejectingReceipt.next_step, "WORKFLOW_CLOSED");
    assert.equal(rejectingReceipt.candidate_revision, 2);

    const afterClosedApproval = structuredClone(rejectingApproval);
    afterClosedApproval.approval_instance_id = "approval_instance_after_close_01";
    afterClosedApproval.expected_candidate_revision = rejectingReceipt.candidate_revision;
    afterClosedApproval.decision = "APPROVE";
    const afterClosedBody = JSON.stringify(afterClosedApproval);
    const afterClosedResponse = await fetch(`${baseUrl}/integrations/feishu/v1/approval-decisions`, {
      method: "POST",
      headers: feishuHeaders(afterClosedBody, "nonce_approval_after_close", {
        idempotencyKey: "idem_approval_after_close_01",
      }),
      body: afterClosedBody,
    });
    assert.equal(afterClosedResponse.status, 409);
    const closedFailure = await afterClosedResponse.json();
    assert.equal(closedFailure.error.code, "CONTENT_VERSION_MISMATCH");
    assert.equal(closedFailure.error.details.reason, "CANDIDATE_WORKFLOW_CLOSED");
    const closedStatus = await fetch(
      `${baseUrl}/integrations/feishu/v1/content-releases/${secondContentReceipt.release_id}`,
      { headers: feishuReadHeaders },
    ).then((response) => response.json());
    assert.equal(closedStatus.candidate_revision, 2,
      "terminal candidate decisions must not increment after closure");

    const reportBody = JSON.stringify(example("feishu-report-job-request.json"));
    const reportResponse = await fetch(`${baseUrl}/integrations/feishu/v1/report-jobs`, {
      method: "POST",
      headers: feishuHeaders(reportBody, "nonce_report_0001", { idempotencyKey: "idem_report_000000001" }),
      body: reportBody,
    });
    assert.equal(reportResponse.status, 202);
    assert.equal(reportResponse.headers.get("retry-after"), "1");
    assert.equal(reportResponse.headers.get("idempotency-replayed"), "false");
    const report = await reportResponse.json();
    assert.equal(
      reportResponse.headers.get("location"),
      `/integrations/feishu/v1/report-jobs/${report.job.job_id}`,
    );
    const reportStatusResponse = await fetch(
      `${baseUrl}/integrations/feishu/v1/report-jobs/${report.job.job_id}`,
      { headers: feishuReadHeaders },
    );
    assert.equal(reportStatusResponse.status, 200);
    assert.deepEqual(await reportStatusResponse.json(), report);

    const missingContent = await fetch(
      `${baseUrl}/integrations/feishu/v1/content-releases/rel_does_not_exist`,
      { headers: feishuReadHeaders },
    );
    assert.equal(missingContent.status, 404);
    assert.equal((await missingContent.json()).error.code, "NOT_FOUND");
  } finally {
    await close(server);
  }
});

test("mock Feishu requests bind body context identities to headers", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  const body = JSON.stringify(example("feishu-learner-query-request.json"));
  try {
    for (const [field, overrides] of [
      ["request_id", { requestId: "req_feishu_mismatch_01", idempotencyKey: "idem_context_request_01" }],
      ["trace_id", { traceId: "trace_feishu_mismatch_01", idempotencyKey: "idem_context_trace_0001" }],
    ]) {
      const response = await fetch(`${baseUrl}/integrations/feishu/v1/learner-queries`, {
        method: "POST",
        headers: feishuHeaders(body, `nonce_context_${field}`, overrides),
        body,
      });
      assert.equal(response.status, 400);
      const error = await response.json();
      assert.equal(error.error.code, "INVALID_REQUEST");
      assert.deepEqual(error.error.details.fields, [field]);
    }

    const identityCases = [
      ["actor.tenant_id", body, {
        authorization: "Bearer tenant_other:feishu_teacher_001",
        idempotencyKey: "idem_context_tenant_0001",
      }],
      ["actor.actor_id", body, {
        authorization: "Bearer tenant_yaya:feishu_teacher_999",
        idempotencyKey: "idem_context_actor_00001",
      }],
      ["actor.actor_type", JSON.stringify({
        ...example("feishu-learner-query-request.json"),
        context: {
          ...example("feishu-learner-query-request.json").context,
          actor: { ...example("feishu-learner-query-request.json").context.actor, actor_type: "operator" },
        },
      }), { idempotencyKey: "idem_context_type_000001" }],
      ["actor.roles", JSON.stringify({
        ...example("feishu-learner-query-request.json"),
        context: {
          ...example("feishu-learner-query-request.json").context,
          actor: {
            ...example("feishu-learner-query-request.json").context.actor,
            roles: ["teacher", "content:approver"],
          },
        },
      }), { idempotencyKey: "idem_context_roles_00001" }],
    ];
    for (const [field, requestBody, overrides] of identityCases) {
      const response = await fetch(`${baseUrl}/integrations/feishu/v1/learner-queries`, {
        method: "POST",
        headers: feishuHeaders(requestBody, `nonce_context_${field.replaceAll(".", "_")}`, overrides),
        body: requestBody,
      });
      assert.equal(response.status, 400);
      const error = await response.json();
      assert.equal(error.error.code, "INVALID_REQUEST");
      assert.deepEqual(error.error.details.fields, [field]);
    }

    const accepted = await fetch(`${baseUrl}/integrations/feishu/v1/learner-queries`, {
      method: "POST",
      headers: feishuHeaders(body, "nonce_context_valid", { idempotencyKey: "idem_context_valid_0001" }),
      body,
    });
    assert.equal(accepted.status, 200);
  } finally {
    await close(server);
  }
});

test("mock Feishu projections preserve requested subject and trace identities", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  try {
    const learnerRequest = example("feishu-learner-query-request.json");
    learnerRequest.context.request_id = "req_feishu_dynamic_001";
    learnerRequest.context.trace_id = "trace_feishu_dynamic_001";
    learnerRequest.learner_ref = "lrn_00000002";
    learnerRequest.requested_fields = ["DATA_FRESHNESS"];
    const learnerBody = JSON.stringify(learnerRequest);
    const learnerResponse = await fetch(`${baseUrl}/integrations/feishu/v1/learner-queries`, {
      method: "POST",
      headers: feishuHeaders(learnerBody, "nonce_dynamic_learner", {
        idempotencyKey: "idem_dynamic_learner_01",
      }),
      body: learnerBody,
    });
    assert.equal(learnerResponse.status, 200);
    const learner = await learnerResponse.json();
    assert.equal(learner.learner_ref, learnerRequest.learner_ref);
    assert.equal(learner.trace_id, learnerRequest.context.trace_id);
    assert.equal("mastery_summary" in learner, false);
    assert.equal("recent_evidence" in learner, false);
    assert.equal("support_needs" in learner, false);

    const classRequest = example("feishu-class-insights-request.json");
    classRequest.context.request_id = "req_feishu_dynamic_002";
    classRequest.context.trace_id = "trace_feishu_dynamic_002";
    classRequest.class_ref = "cls_00000002";
    classRequest.dimensions = ["CONCEPT_MASTERY"];
    classRequest.privacy.minimum_cohort_size = 10;
    const classBody = JSON.stringify(classRequest);
    const classResponse = await fetch(`${baseUrl}/integrations/feishu/v1/class-insights`, {
      method: "POST",
      headers: feishuHeaders(classBody, "nonce_dynamic_class", {
        idempotencyKey: "idem_dynamic_class_0001",
      }),
      body: classBody,
    });
    assert.equal(classResponse.status, 200);
    const classInsights = await classResponse.json();
    assert.equal(classInsights.class_ref, classRequest.class_ref);
    assert.equal(classInsights.trace_id, classRequest.context.trace_id);
    assert.equal(classInsights.privacy.minimum_cohort_size, 10);
    assert.equal(classInsights.privacy.effective_minimum_cohort_size, 10);
    assert.ok(classInsights.insights.every((insight) => classRequest.dimensions.includes(insight.dimension)));
    assert.ok(classInsights.insights.every((insight) => insight.suppressed));
  } finally {
    await close(server);
  }
});

test("mock audits every OpenAPI-marked access outcome without retaining sensitive payloads", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  const learnerRequest = example("feishu-learner-query-request.json");
  learnerRequest.learner_ref = "lrn_sensitive_00000001";
  const learnerBody = JSON.stringify(learnerRequest);
  const classBody = JSON.stringify(example("feishu-class-insights-request.json"));
  const evidenceId = example("feishu-evidence-response.json").evidence_ref.evidence_id;
  try {
    const allowed = await fetch(`${baseUrl}/integrations/feishu/v1/learner-queries`, {
      method: "POST",
      headers: feishuHeaders(learnerBody, "nonce_audit_allowed_01", {
        idempotencyKey: "idem_audit_allowed_0001",
      }),
      body: learnerBody,
    });
    assert.equal(allowed.status, 200);

    const denied = await fetch(`${baseUrl}/integrations/feishu/v1/class-insights`, {
      method: "POST",
      headers: feishuHeaders(classBody, "nonce_audit_denied_001", {
        authorization: "Bearer tenant_yaya:student_0001",
        idempotencyKey: "idem_audit_denied_00001",
      }),
      body: classBody,
    });
    assert.equal(denied.status, 403);
    assert.equal((await denied.json()).error.code, "AUTHORIZATION_DENIED");

    const failed = await fetch(
      `${baseUrl}/integrations/feishu/v1/evidence/${evidenceId}?purpose=NOT_OPENAPI`,
      {
        headers: {
          Authorization: "Bearer tenant_yaya:operator_0001",
          "X-Request-Id": "req_audit_failed_0001",
          "X-Trace-Id": "trace_audit_failed_0001",
          "X-Schema-Version": "1.0.0",
        },
      },
    );
    assert.equal(failed.status, 400);
    assert.equal((await failed.json()).error.code, "INVALID_REQUEST");

    const ordinary = await fetch(`${baseUrl}/v1/bootstrap`, { headers: readHeaders });
    assert.equal(ordinary.status, 200);

    const records = server.getAuditRecords();
    assert.equal(records.length, 3, "ordinary routes must not create access audit records");
    assert.deepEqual(
      records.map(({ operation, outcome, error_code }) => [operation, outcome, error_code]),
      [
        ["queryLearnerProjectionFromFeishu", "ALLOWED", null],
        ["queryClassInsightsFromFeishu", "DENIED", "AUTHORIZATION_DENIED"],
        ["getRedactedEvidenceForFeishu", "FAILED", "INVALID_REQUEST"],
      ],
    );

    const learnerAudit = records[0];
    assert.equal(learnerAudit.actor.actor_id, learnerRequest.context.actor.actor_id);
    assert.equal(learnerAudit.request_id, learnerRequest.context.request_id);
    assert.equal(learnerAudit.correlation_id, learnerRequest.context.correlation_id);
    assert.equal(learnerAudit.trace_id, learnerRequest.context.trace_id);
    assert.equal(learnerAudit.resource_type, "LEARNER_PROJECTION");
    assert.equal(learnerAudit.purpose, learnerRequest.purpose);
    assert.match(learnerAudit.subject_hash, /^[a-f0-9]{64}$/u);
    assert.equal(learnerAudit.redacted, true);
    assert.equal(records[1].actor.actor_id, "student_0001");
    assert.equal(records[2].resource_id, evidenceId);
    assert.equal(records[2].purpose, "NOT_OPENAPI");

    const serializedAudit = JSON.stringify(records);
    assert.doesNotMatch(serializedAudit, /lrn_sensitive_00000001/u);
    assert.doesNotMatch(serializedAudit, /"(?:learner_ref|requested_fields|time_range|raw_payload|request_body)"/u);

    const { documents } = loadDocuments();
    const auditSchemaPath = resolve(AGENT_ROOT, "contracts/schemas/common/audit-record.schema.json");
    const auditSchema = documents.get(auditSchemaPath);
    for (const [index, record] of records.entries()) {
      assertSchema(record, auditSchema, auditSchemaPath, documents, `audit record ${index}`);
    }

    records.pop();
    assert.equal(server.getAuditRecords().length, 3, "audit snapshots must not expose mutable state");
  } finally {
    await close(server);
  }
});

test("mock Feishu evidence GET requires one OpenAPI purpose value", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, feishuSecret: FEISHU_SECRET });
  const baseUrl = await listen(server);
  const evidenceId = example("feishu-evidence-response.json").evidence_ref.evidence_id;
  const headers = {
    Authorization: "Bearer tenant_yaya:operator_0001",
    "X-Request-Id": "req_feishu_evidence_01",
    "X-Trace-Id": "trace_feishu_evidence_01",
    "X-Schema-Version": "1.0.0",
  };
  try {
    const denied = await fetch(
      `${baseUrl}/integrations/feishu/v1/evidence/${evidenceId}?purpose=TEACHER_SUPPORT`,
      { headers: { ...headers, Authorization: "Bearer tenant_yaya:student_0001" } },
    );
    assert.equal(denied.status, 403);
    assert.equal((await denied.json()).error.code, "AUTHORIZATION_DENIED");

    for (const query of ["", "?purpose=NOT_OPENAPI", "?purpose=TEACHER_SUPPORT&purpose=LEARNING_REVIEW"]) {
      const response = await fetch(`${baseUrl}/integrations/feishu/v1/evidence/${evidenceId}${query}`, { headers });
      assert.equal(response.status, 400);
      assert.equal((await response.json()).error.code, "INVALID_REQUEST");
    }
    const response = await fetch(
      `${baseUrl}/integrations/feishu/v1/evidence/${evidenceId}?purpose=TEACHER_SUPPORT`,
      { headers },
    );
    assert.equal(response.status, 200);
    assert.equal((await response.json()).evidence_ref.evidence_id, evidenceId);
  } finally {
    await close(server);
  }
});
