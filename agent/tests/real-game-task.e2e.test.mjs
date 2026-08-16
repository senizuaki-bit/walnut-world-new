import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createMockServer } from "../scripts/mock-server.mjs";
import { canonicalJsonSha256V1 } from "../src/canonical-json.mjs";

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const AGENT_ROOT = resolve(TEST_DIR, "..");
const FIXED_NOW = Date.parse("2026-08-07T14:00:00Z");

function example(name) {
  return JSON.parse(readFileSync(resolve(AGENT_ROOT, "contracts", "examples", name), "utf8")).value;
}

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
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

/**
 * Reference consumer used only by end-to-end contract scenarios.
 * Every call creates a new HTTP-attempt identity and verifies that the server
 * echoes it, so tests cannot accidentally hide origin/attempt confusion by
 * reusing one global header object.
 */
class ScenarioGameClient {
  #baseUrl;
  #attempt = 0;

  constructor(baseUrl) {
    this.#baseUrl = baseUrl;
  }

  async request(method, path, { body, idempotencyKey, expectedStatus = 200 } = {}) {
    this.#attempt += 1;
    const suffix = String(this.#attempt).padStart(8, "0");
    const identity = {
      request_id: `req_real_task_${suffix}`,
      trace_id: `trace_real_task_${suffix}`,
      correlation_id: `corr_real_task_${suffix}`,
    };
    const headers = {
      Authorization: "Bearer tenant_yaya:student_0001",
      "X-Request-Id": identity.request_id,
      "X-Trace-Id": identity.trace_id,
      "X-Correlation-Id": identity.correlation_id,
      "X-Schema-Version": "1.0.0",
    };
    let rawBody;
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      headers["Idempotency-Key"] = idempotencyKey;
      rawBody = JSON.stringify(body);
    }
    const response = await fetch(`${this.#baseUrl}${path}`, { method, headers, body: rawBody });
    const payload = await response.json();
    assert.equal(response.status, expectedStatus, `${method} ${path}: ${JSON.stringify(payload)}`);
    assert.equal(response.headers.get("x-request-id"), identity.request_id);
    assert.equal(response.headers.get("x-trace-id"), identity.trace_id);
    assert.equal(response.headers.get("x-correlation-id"), identity.correlation_id);
    return { response, payload, identity, rawBody };
  }
}

test("real watering task builds, activates, runs, explains and commits exactly once", async () => {
  const server = createMockServer({ now: () => FIXED_NOW, logger: () => {} });
  const baseUrl = await listen(server);
  const client = new ScenarioGameClient(baseUrl);
  try {
    const bootstrapCall = await client.request("GET", "/v1/bootstrap");
    const bootstrap = bootstrapCall.payload;
    const initialRevision = bootstrap.world.revision;
    const initialSequence = bootstrap.world.last_event_sequence;
    assert.equal(bootstrap.limits.max_source_files, 32);
    assert.equal(bootstrap.limits.max_source_bytes, 1_048_576);
    const initialSnapshot = (
      await client.request("GET", `/v1/worlds/${bootstrap.world.world_id}/snapshot`)
    ).payload;
    const initialHydration = initialSnapshot.state.plots[0].hydration;

    const source = [
      "#include <cstdint>",
      "struct Plot { bool dry; std::int32_t hydration; };",
      "std::int32_t water_if_dry(Plot& plot) {",
      "  if (!plot.dry) return 0;",
      "  plot.dry = false;",
      "  plot.hydration += 200;",
      "  return 1;",
      "}",
      "int main() { Plot plot{true, 7000}; return water_if_dry(plot) == 1 ? 0 : 1; }",
      "",
    ].join("\n");
    const buildRequest = example("game-skill-build-create-request.json");
    buildRequest.display_name = "真实任务：给干燥土地浇水";
    buildRequest.source_bundle.files[0].content = source;
    buildRequest.source_bundle.files[0].content_sha256 = sha256(source);
    const buildAccepted = await client.request("POST", "/v1/skill-builds", {
      body: buildRequest,
      idempotencyKey: "idem_real_task_build_0001",
      expectedStatus: 202,
    });
    assert.equal(buildAccepted.payload.trace_id, buildAccepted.identity.trace_id);
    assert.equal(buildAccepted.response.headers.get("idempotency-replayed"), "false");

    const buildCommandCall = await client.request(
      "GET", `/v1/commands/${buildAccepted.payload.command_id}`,
    );
    const buildCommand = buildCommandCall.payload;
    assert.equal(buildCommand.status, "APPLIED");
    assert.equal(buildCommand.result.resource_type, "SKILL_BUILD");
    assert.equal(buildCommand.request_context.request_id, buildAccepted.identity.request_id);
    assert.notEqual(buildCommand.request_context.request_id, buildCommandCall.identity.request_id);

    const buildResourceCall = await client.request("GET", buildCommand.result.resource_url);
    const build = buildResourceCall.payload;
    assert.equal(build.status, "CERTIFIED");
    assert.equal(build.skill_id, buildRequest.skill_id);
    assert.equal(build.request_context.trace_id, buildAccepted.identity.trace_id);
    assert.ok(build.certification.certification_id);
    assert.ok(build.artifact.artifact_sha256);

    const activationRequest = example("game-skill-activation-request.json");
    const activationAccepted = await client.request(
      "POST", `/v1/skill-versions/${build.skill_version_id}/activations`, {
        body: activationRequest,
        idempotencyKey: "idem_real_task_activation_01",
        expectedStatus: 202,
      },
    );
    const activationCommand = (
      await client.request("GET", `/v1/commands/${activationAccepted.payload.command_id}`)
    ).payload;
    const activation = (await client.request("GET", activationCommand.result.resource_url)).payload;
    assert.equal(activation.skill_version_id, build.skill_version_id);
    assert.equal(activation.certification_id, build.certification.certification_id);
    assert.equal(activation.registry_revision, activation.previous_registry_revision + 1);

    const sessionRequest = example("game-agent-session-create-request.json");
    sessionRequest.expected_world_revision = initialRevision;
    const sessionAccepted = await client.request("POST", "/v1/agent-sessions", {
      body: sessionRequest,
      idempotencyKey: "idem_real_task_session_0001",
      expectedStatus: 202,
    });
    const sessionCommand = (
      await client.request("GET", `/v1/commands/${sessionAccepted.payload.command_id}`)
    ).payload;
    const session = (await client.request("GET", sessionCommand.result.resource_url)).payload;
    assert.equal(session.world_id, bootstrap.world.world_id);
    assert.equal(session.last_turn_sequence, 0);

    const turnRequest = example("game-agent-turn-create-request.json");
    turnRequest.turn_id = "turn_real_task_0001";
    turnRequest.expected_world_revision = initialRevision;
    turnRequest.client_state.last_event_sequence = initialSequence;
    turnRequest.client_state.client_turn_sequence = 1;
    turnRequest.skill_bindings = [{
      skill_id: build.skill_id,
      skill_version_id: build.skill_version_id,
      artifact_sha256: build.artifact.artifact_sha256,
      certification_id: build.certification.certification_id,
    }];
    const turnIdempotencyKey = "idem_real_task_turn_000001";
    const turnAccepted = await client.request(
      "POST", `/v1/agent-sessions/${session.session_id}/turns`, {
        body: turnRequest,
        idempotencyKey: turnIdempotencyKey,
        expectedStatus: 202,
      },
    );

    // A network retry uses a fresh HTTP-attempt identity but the exact same
    // body/key. It must replay the original receipt and must not commit twice.
    const turnReplay = await client.request(
      "POST", `/v1/agent-sessions/${session.session_id}/turns`, {
        body: turnRequest,
        idempotencyKey: turnIdempotencyKey,
        expectedStatus: 202,
      },
    );
    assert.equal(turnReplay.response.headers.get("idempotency-replayed"), "true");
    assert.equal(turnReplay.payload.command_id, turnAccepted.payload.command_id);
    assert.equal(turnReplay.payload.trace_id, turnAccepted.payload.trace_id);
    assert.notEqual(turnReplay.response.headers.get("x-trace-id"), turnReplay.payload.trace_id);

    const turnCommand = (
      await client.request("GET", `/v1/commands/${turnAccepted.payload.command_id}`)
    ).payload;
    assert.equal(turnCommand.status, "APPLIED");
    assert.equal(turnCommand.stage, "COMPLETE");
    assert.equal(turnCommand.result.result_type, "WORLD_COMMIT");
    assert.equal(turnCommand.result.previous_revision, initialRevision);
    assert.equal(turnCommand.result.world_revision, initialRevision + 1);
    assert.ok(turnCommand.links.run);

    const run = (await client.request("GET", turnCommand.links.run)).payload;
    assert.equal(run.command_id, turnCommand.command_id);
    assert.equal(run.session_id, session.session_id);
    assert.equal(run.turn_id, turnRequest.turn_id);
    assert.equal(run.run_id, run.agent_feedback.run_id);
    assert.equal(run.agent_feedback.session_id, run.session_id);
    assert.equal(run.agent_feedback.turn_id, run.turn_id);
    assert.equal(run.agent_feedback.command_id, turnCommand.command_id);
    assert.equal(run.agent_feedback.turn_id, turnRequest.turn_id);
    assert.equal(run.agent_feedback.source, "provider");
    assert.equal(run.agent_feedback.degraded, false);
    assert.equal(run.agent_feedback.fallback_reason, null);
    assert.match(run.agent_feedback.message, /浇水/u);
    assert.equal(run.world_application.receipt.world_revision, initialRevision + 1);
    assert.equal(run.world_application.receipt.previous_revision, initialRevision);
    assert.equal(
      run.world_application.receipt.previous_revision,
      turnCommand.result.previous_revision,
    );
    assert.equal(run.world_application.receipt.world_revision, turnCommand.result.world_revision);
    assert.equal(
      run.world_application.receipt.first_event_sequence,
      turnCommand.result.first_event_sequence,
    );
    assert.equal(
      run.world_application.receipt.last_event_sequence,
      turnCommand.result.last_event_sequence,
    );
    assert.deepEqual(run.skill, turnRequest.skill_bindings[0]);
    assert.deepEqual(turnCommand.evidence_refs, run.evidence_refs);
    assert.deepEqual(run.agent_feedback.evidence_refs, run.evidence_refs);
    assert.equal(run.evidence_refs.length, 1);

    const evidenceRef = run.evidence_refs[0];
    const evidenceCall = await client.request("GET", `/v1/evidence/${evidenceRef.evidence_id}`);
    const evidence = evidenceCall.payload;
    assert.equal(evidenceCall.response.headers.get("etag"), `"${evidenceRef.sha256}"`);
    assert.deepEqual(evidence.evidence_ref, evidenceRef);
    assert.equal(evidence.request_context.actor.actor_id, "student_0001");
    assert.equal(evidence.source.command_id, turnCommand.command_id);
    assert.equal(evidence.source.source_id, run.world_application.receipt.world_id);
    assert.equal(evidence.source.world_id, run.world_application.receipt.world_id);
    assert.equal(evidence.occurred_at, run.world_application.receipt.committed_at);
    assert.equal(evidence.recorded_at, run.world_application.receipt.committed_at);
    assert.equal(evidence.integrity.payload_sha256, evidenceRef.sha256);
    assert.equal(evidenceRef.sha256, canonicalJsonSha256V1(evidence.payload));
    assert.deepEqual(evidence.payload, {
      evidence_kind: "WORLD_COMMIT",
      world_id: run.world_application.receipt.world_id,
      previous_revision: run.world_application.receipt.previous_revision,
      world_revision: run.world_application.receipt.world_revision,
      first_event_sequence: run.world_application.receipt.first_event_sequence,
      last_event_sequence: run.world_application.receipt.last_event_sequence,
      state_hash: run.world_application.receipt.state_hash,
    });
    assert.equal(evidence.versions.skill_version, run.skill.skill_version_id);
    assert.equal(evidence.versions.artifact_sha256, run.skill.artifact_sha256);

    const snapshotCall = await client.request(
      "GET", `/v1/worlds/${bootstrap.world.world_id}/snapshot`,
    );
    const snapshot = snapshotCall.payload;
    assert.equal(snapshot.revision, initialRevision + 1);
    assert.equal(snapshot.last_event_sequence, initialSequence + 2);
    assert.equal(snapshot.revision, run.world_application.receipt.world_revision);
    assert.equal(snapshot.state_hash, run.world_application.receipt.state_hash);
    assert.equal(snapshot.last_event_sequence, run.world_application.receipt.last_event_sequence);
    assert.equal(snapshotCall.response.headers.get("x-world-revision"), String(snapshot.revision));
    assert.equal(snapshot.state.plots[0].last_updated_event_sequence, snapshot.last_event_sequence);
    assert.equal(snapshot.state.plots[0].hydration, Math.min(10_000, initialHydration + 200));

    const events = (
      await client.request(
        "GET",
        `/v1/worlds/${bootstrap.world.world_id}/events?after_sequence=${initialSequence}&limit=500`,
      )
    ).payload;
    assert.deepEqual(events.events.map((event) => event.sequence), [initialSequence + 1, initialSequence + 2]);
    assert.equal(events.events[0].sequence, run.world_application.receipt.first_event_sequence);
    assert.equal(events.events.at(-1).sequence, run.world_application.receipt.last_event_sequence);
    assert.equal(events.snapshot_revision, run.world_application.receipt.world_revision);
    assert.ok(events.events.every((event) => event.command_id === turnCommand.command_id));
    assert.equal(events.next_after_sequence, snapshot.last_event_sequence);

    // A new key is a new command, so the stale revision must be rejected and
    // cannot be confused with the safe replay above.
    const staleTurn = await client.request(
      "POST", `/v1/agent-sessions/${session.session_id}/turns`, {
        body: turnRequest,
        idempotencyKey: "idem_real_task_turn_stale01",
        expectedStatus: 409,
      },
    );
    assert.equal(staleTurn.payload.error.code, "WORLD_REVISION_CONFLICT");
    const afterFailure = (
      await client.request("GET", `/v1/worlds/${bootstrap.world.world_id}/snapshot`)
    ).payload;
    assert.equal(afterFailure.revision, snapshot.revision);
    assert.equal(afterFailure.last_event_sequence, snapshot.last_event_sequence);
  } finally {
    await close(server);
  }
});

test("run feedback owner and evidence corruption fail loudly before reaching the client", async (context) => {
  const corruptions = {
    run_id: (payload) => ({
      ...payload,
      agent_feedback: { ...payload.agent_feedback, run_id: "run_corrupted_0001" },
    }),
    session_id: (payload) => ({
      ...payload,
      agent_feedback: { ...payload.agent_feedback, session_id: "session_corrupted_0001" },
    }),
    turn_id: (payload) => ({
      ...payload,
      agent_feedback: { ...payload.agent_feedback, turn_id: "turn_corrupted_0001" },
    }),
    evidence_refs: (payload) => ({
      ...payload,
      agent_feedback: { ...payload.agent_feedback, evidence_refs: [] },
    }),
    duplicate_evidence_id: (payload) => {
      const duplicate = {
        ...payload.evidence_refs[0],
        created_at: "2026-08-07T14:00:01Z",
        sha256: "f".repeat(64),
      };
      const evidenceRefs = [...payload.evidence_refs, duplicate];
      return {
        ...payload,
        evidence_refs: evidenceRefs,
        agent_feedback: { ...payload.agent_feedback, evidence_refs: evidenceRefs },
      };
    },
    inverted_event_range: (payload) => ({
      ...payload,
      world_application: {
        ...payload.world_application,
        receipt: {
          ...payload.world_application.receipt,
          first_event_sequence: 800,
          last_event_sequence: 700,
        },
      },
    }),
  };
  for (const [label, corrupt] of Object.entries(corruptions)) {
    await context.test(label, async () => {
      const server = createMockServer({
        now: () => FIXED_NOW,
        logger: () => {},
        responseTransform: (payload, responseContext) => {
          if (responseContext.schema !== "gameRun" || payload?.agent_feedback === null) return payload;
          return corrupt(payload);
        },
      });
      const baseUrl = await listen(server);
      const client = new ScenarioGameClient(baseUrl);
      try {
        const result = await client.request("GET", "/v1/runs/run_water_0001", {
          expectedStatus: 500,
        });
        assert.equal(result.payload.error.code, "INTERNAL_ERROR");
        assert.equal(result.payload.error.details.reason, "RESPONSE_SEMANTIC_INVARIANT");
        assert.equal(result.payload.error.details.invariant_code, "INVARIANT_VIOLATION");
        assert.equal(result.payload.error.details.schema, "gameRun");
      } finally {
        await close(server);
      }
    });
  }
});

test("evidence payload tampering is rejected even when every field remains schema-valid", async () => {
  const server = createMockServer({
    now: () => FIXED_NOW,
    logger: () => {},
    responseTransform: (payload, responseContext) => {
      if (responseContext.schema !== "gameEvidence") return payload;
      return {
        ...payload,
        payload: { ...payload.payload, state_hash: "0".repeat(64) },
      };
    },
  });
  const baseUrl = await listen(server);
  const client = new ScenarioGameClient(baseUrl);
  try {
    const result = await client.request("GET", "/v1/evidence/evidence_world_00000001", {
      expectedStatus: 500,
    });
    assert.equal(result.payload.error.code, "INTERNAL_ERROR");
    assert.equal(result.payload.error.details.reason, "RESPONSE_SEMANTIC_INVARIANT");
    assert.equal(result.payload.error.details.schema, "gameEvidence");
  } finally {
    await close(server);
  }
});

test("canonical resource identities pass and coherent outbound relabeling fails over HTTP", async (context) => {
  await context.test("canonical resources", async () => {
    const server = createMockServer({
      now: () => FIXED_NOW,
      logger: () => {},
      // Canonical equality is semantic for object keys, not insertion-order based.
      responseTransform: (payload) => Object.fromEntries(Object.entries(payload).reverse()),
    });
    const baseUrl = await listen(server);
    const client = new ScenarioGameClient(baseUrl);
    try {
      const accepted = await client.request("POST", "/v1/skill-builds", {
        body: example("game-skill-build-create-request.json"),
        idempotencyKey: "idem_identity_positive_0001",
        expectedStatus: 202,
      });
      const command = (
        await client.request("GET", `/v1/commands/${accepted.payload.command_id}`)
      ).payload;
      assert.equal(command.command_id, accepted.payload.command_id);
      const run = (await client.request("GET", "/v1/runs/run_water_0001")).payload;
      assert.equal(run.run_id, "run_water_0001");
      assert.equal(run.agent_feedback.run_id, run.run_id);
      const evidence = (
        await client.request("GET", "/v1/evidence/evidence_world_00000001")
      ).payload;
      assert.equal(evidence.evidence_ref.evidence_id, "evidence_world_00000001");
    } finally {
      await close(server);
    }
  });

  const relabelCases = [
    {
      label: "command id",
      schema: "gameCommand",
      path: "/v1/commands/cmd_game_00000001",
      expectedField: "command_id",
      relabel: (payload) => ({ ...payload, command_id: "cmd_relabelled_00000001" }),
    },
    {
      label: "command resource result",
      schema: "gameCommand",
      path: "/v1/commands/cmd_game_00000001",
      expectedField: "payload",
      relabel: (payload) => ({
        ...payload,
        result: {
          ...payload.result,
          resource_id: "build_relabelled_0001",
          resource_url: "/v1/skill-builds/build_relabelled_0001",
        },
      }),
    },
    {
      label: "run owners at both levels",
      schema: "gameRun",
      path: "/v1/runs/run_water_0001",
      expectedField: "run_id",
      relabel: (payload) => {
        const owner = {
          run_id: "run_relabelled_0001",
          session_id: "session_relabelled_0001",
          turn_id: "turn_relabelled_0001",
          command_id: "cmd_relabelled_00000001",
        };
        return {
          ...payload,
          ...owner,
          agent_feedback: { ...payload.agent_feedback, ...owner },
        };
      },
    },
    {
      label: "run evidence set at both levels",
      schema: "gameRun",
      path: "/v1/runs/run_water_0001",
      expectedField: "evidence_refs",
      relabel: (payload) => {
        const evidenceRefs = [{
          ...payload.evidence_refs[0],
          evidence_id: "evidence_relabelled_0001",
        }];
        return {
          ...payload,
          evidence_refs: evidenceRefs,
          agent_feedback: { ...payload.agent_feedback, evidence_refs: evidenceRefs },
        };
      },
    },
    {
      label: "run world receipt",
      schema: "gameRun",
      path: "/v1/runs/run_water_0001",
      expectedField: "payload",
      relabel: (payload) => ({
        ...payload,
        world_application: {
          ...payload.world_application,
          receipt: {
            ...payload.world_application.receipt,
            world_id: "world_relabelled_0001",
            previous_revision: 900,
            world_revision: 901,
            first_event_sequence: 5000,
            last_event_sequence: 5001,
            state_hash: "0".repeat(64),
          },
        },
      }),
    },
    {
      label: "evidence id",
      schema: "gameEvidence",
      path: "/v1/evidence/evidence_world_00000001",
      expectedField: "evidence_ref",
      relabel: (payload) => ({
        ...payload,
        evidence_ref: {
          ...payload.evidence_ref,
          evidence_id: "evidence_relabelled_0001",
        },
      }),
    },
  ];

  for (const testCase of relabelCases) {
    await context.test(testCase.label, async () => {
      const server = createMockServer({
        now: () => FIXED_NOW,
        logger: () => {},
        responseTransform: (payload, responseContext) => (
          responseContext.schema === testCase.schema ? testCase.relabel(payload) : payload
        ),
      });
      const baseUrl = await listen(server);
      const client = new ScenarioGameClient(baseUrl);
      try {
        let requestPath = testCase.path;
        if (testCase.schema === "gameCommand") {
          const accepted = await client.request("POST", "/v1/skill-builds", {
            body: example("game-skill-build-create-request.json"),
            idempotencyKey: "idem_identity_command_0001",
            expectedStatus: 202,
          });
          requestPath = `/v1/commands/${accepted.payload.command_id}`;
        }
        const result = await client.request("GET", requestPath, { expectedStatus: 500 });
        assert.equal(result.payload.error.code, "INTERNAL_ERROR");
        assert.equal(
          result.payload.error.details.reason,
          "RESPONSE_RESOURCE_IDENTITY_MISMATCH",
        );
        assert.equal(result.payload.error.details.schema, testCase.schema);
        assert.ok(result.payload.error.details.fields.includes(testCase.expectedField));
      } finally {
        await close(server);
      }
    });
  }
});
