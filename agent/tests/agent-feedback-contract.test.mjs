import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import {
  assertSchema,
  loadDocuments,
  PROJECT_ROOT,
} from "../scripts/validate-contracts.mjs";
import {
  assertAgentTurnFeedbackReadyEvent,
  SemanticInvariantError,
} from "../src/semantic-invariants.mjs";

function json(path) {
  return JSON.parse(readFileSync(resolve(PROJECT_ROOT, path), "utf8"));
}

const SCHEMA_PATH = resolve(
  PROJECT_ROOT,
  "contracts/schemas/game/agent-turn-feedback.schema.json",
);

test("agent feedback wire schema is closed and publishes complete reconciliation identity", () => {
  const schema = json("contracts/schemas/game/agent-turn-feedback.schema.json");
  assert.equal(schema.additionalProperties, false);
  assert.deepEqual(schema.required, [
    "session_id", "turn_id", "command_id", "run_id", "message_key", "message",
    "source", "degraded", "fallback_reason", "evidence_refs", "completed_at",
  ]);
  assert.deepEqual(schema["x-invariants"], [
    "command_id == containing event envelope command_id",
    "degraded == false iff source == provider and fallback_reason == null",
    "degraded == true iff source == provider_fallback and fallback_reason is a non-empty machine-readable code",
    "session_id + turn_id identifies the accepted client turn; run_id identifies its sandbox run when one exists",
    "evidence_refs contains at most one immutable reference for each evidence_id",
  ]);
});

test("provider and fallback feedback are executable disjoint wire variants", () => {
  const { documents } = loadDocuments();
  const schema = documents.get(SCHEMA_PATH);
  const provider = json("contracts/examples/runtime-agent-turn-feedback-ready.json").value.payload;
  const fallback = {
    ...provider,
    run_id: null,
    message_key: "agent.turn.fallback",
    message: "这次我先给你一个安全提示，请稍后再试。",
    source: "provider_fallback",
    degraded: true,
    fallback_reason: "MODEL_OUTPUT_INVALID",
    evidence_refs: [],
  };
  for (const value of [provider, fallback]) {
    assert.doesNotThrow(() => assertSchema(value, schema, SCHEMA_PATH, documents));
  }
  for (const invalid of [
    { ...provider, source: "provider_fallback" },
    { ...provider, fallback_reason: "MODEL_OUTPUT_INVALID" },
    { ...fallback, source: "provider" },
    { ...fallback, fallback_reason: null },
    { ...fallback, fallback_reason: "" },
    { ...provider, silent_extra_field: true },
  ]) {
    assert.throws(() => assertSchema(invalid, schema, SCHEMA_PATH, documents));
  }
});

test("AsyncAPI message name, event_type, payload schema and fixture cannot drift", () => {
  const asyncApi = json("contracts/asyncapi/runtime-events.asyncapi.json");
  const message = asyncApi.components.messages.AgentTurnFeedbackReady;
  const event = asyncApi.components.schemas.AgentTurnFeedbackReadyEvent;
  const specialization = event.allOf.find((branch) => branch.properties?.event_type?.const);
  const fixture = json("contracts/examples/runtime-agent-turn-feedback-ready.json").value;
  assert.equal(message.name, "agent.turn.feedback_ready");
  assert.equal(specialization.properties.event_type.const, message.name);
  assert.equal(fixture.event_type, message.name);
  assert.equal(fixture.payload.command_id, fixture.command_id);
  assert.equal(
    asyncApi.components.schemas.AgentTurnFeedbackReadyPayload.$ref,
    "../schemas/game/agent-turn-feedback.schema.json",
  );
  assert.equal(
    asyncApi.components.schemas.AgentTurnFeedbackReadyEvent.$ref,
    "../schemas/game/agent-turn-feedback-ready-event.schema.json",
  );
});

test("JavaScript producers reject silent feedback mis-linking and discriminator contradictions", () => {
  const valid = json("contracts/examples/runtime-agent-turn-feedback-ready.json").value;
  assert.doesNotThrow(() => assertAgentTurnFeedbackReadyEvent(valid));
  const fallback = structuredClone(valid);
  Object.assign(fallback.payload, {
    run_id: null,
    source: "provider_fallback",
    degraded: true,
    fallback_reason: "MODEL_OUTPUT_INVALID",
  });
  assert.doesNotThrow(() => assertAgentTurnFeedbackReadyEvent(fallback));

  const invalid = [
    { value: structuredClone(valid), code: "INVARIANT_VIOLATION", mutate(value) {
      value.payload.command_id = "cmd_feedback_other_001";
    } },
    { value: structuredClone(valid), code: "INVARIANT_VIOLATION", mutate(value) {
      value.payload.source = "provider_fallback";
    } },
    { value: structuredClone(fallback), code: "INVARIANT_VIOLATION", mutate(value) {
      value.payload.fallback_reason = null;
    } },
    { value: structuredClone(valid), code: "INVALID_REQUEST", mutate(value) {
      value.payload.session_id = "";
    } },
    { value: structuredClone(valid), code: "INVARIANT_VIOLATION", mutate(value) {
      value.stream_id = "agent-session:session_other_001";
    } },
    { value: structuredClone(valid), code: "INVARIANT_VIOLATION", mutate(value) {
      value.payload.evidence_refs.push({
        ...value.payload.evidence_refs[0],
        sha256: "f".repeat(64),
      });
    } },
  ];
  for (const testCase of invalid) {
    testCase.mutate(testCase.value);
    assert.throws(
      () => assertAgentTurnFeedbackReadyEvent(testCase.value),
      (error) => error instanceof SemanticInvariantError && error.code === testCase.code,
    );
  }
});
