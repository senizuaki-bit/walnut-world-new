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
  applyProductSkillPatch,
  assertProductAgentInteraction,
  assertProductAgentInteractionPage,
  assertProductAgentInteractionSourceReceipt,
  assertProductExampleRelationships,
  assertProductPatchDecisionRequest,
  assertProductPatchDecisionReceipt,
  assertProductSessionWorkspace,
  assertProductSkillDraft,
  assertProductSkillPatch,
  assertProductWriteReconciliation,
  computeProductFeedbackSha256,
  computeProductInteractionSourceSha256,
  computeProductPatchSha256,
  ProductExperienceInvariantError,
} from "../scripts/product-experience-invariants.mjs";
import { canonicalJsonSha256V1 } from "../src/canonical-json.mjs";

function json(relativePath) {
  return JSON.parse(readFileSync(resolve(PROJECT_ROOT, relativePath), "utf8"));
}

const { documents } = loadDocuments();

function schema(relativePath) {
  const absolutePath = resolve(PROJECT_ROOT, relativePath);
  return { absolutePath, value: documents.get(absolutePath) };
}

function example(name) {
  return json(`contracts/examples/${name}.json`).value;
}

function productEntry(schemaName, value) {
  return {
    schemaFile: resolve(
      PROJECT_ROOT,
      `contracts/schemas/product-experience/${schemaName}.schema.json`,
    ),
    value,
  };
}

function gameEntry(schemaName, value) {
  return {
    schemaFile: resolve(
      PROJECT_ROOT,
      `contracts/schemas/game/${schemaName}.schema.json`,
    ),
    value,
  };
}

function canonicalGameRelationshipEntries() {
  return [
    gameEntry("agent-session", example("game-agent-session")),
    gameEntry("command", example("game-command")),
    gameEntry("run", example("game-run")),
    gameEntry("evidence", example("game-evidence")),
    gameEntry("agent-turn-feedback-ready-event", example("product-agent-feedback-source-event")),
    gameEntry("world-event-page", example("game-world-event-page")),
    productEntry("agent-interaction-source-receipt", example("product-agent-interaction-source-receipt")),
  ];
}

function canonicalRejectRelationshipEntries() {
  return [
    gameEntry("agent-session", example("game-agent-session")),
    gameEntry("command", example("product-reject-game-command-source")),
    gameEntry("agent-turn-feedback-ready-event", example("product-reject-agent-feedback-source-event")),
    productEntry(
      "agent-interaction-source-receipt",
      example("product-agent-interaction-source-receipt-reject"),
    ),
  ];
}

function assertValid(value, schemaPath) {
  const contract = schema(schemaPath);
  assert.doesNotThrow(() => assertSchema(value, contract.value, contract.absolutePath, documents));
}

function assertInvalid(value, schemaPath) {
  const contract = schema(schemaPath);
  assert.throws(() => assertSchema(value, contract.value, contract.absolutePath, documents));
}

function assertSemanticInvalid(action) {
  assert.throws(action, (error) => error instanceof ProductExperienceInvariantError);
}

function rebindProjectionSourceFeedback(interaction) {
  interaction.projection_source.feedback_sha256 = interaction.feedback_event.feedback_sha256;
  interaction.projection_source.source_sha256 = computeProductInteractionSourceSha256(
    interaction.projection_source,
  );
}

function resolveLocal(document, candidate) {
  let value = candidate;
  const seen = new Set();
  while (value?.$ref?.startsWith("#/")) {
    assert.ok(!seen.has(value.$ref), `cyclic local reference ${value.$ref}`);
    seen.add(value.$ref);
    value = value.$ref.slice(2).split("/").reduce((node, segment) => node[segment], document);
  }
  return value;
}

test("Product Experience publishes exactly the seven additive projection operations", () => {
  const api = json("contracts/openapi/product-experience.openapi.json");
  const expected = [
    "getProductAgentInteraction",
    "getProductContentUnit",
    "getProductSessionWorkspace",
    "getProductSkillDraft",
    "listProductAgentInteractions",
    "recordProductPatchDecision",
    "upsertProductSkillDraft",
  ];
  const actual = [];
  const commonParameters = ["RequestId", "TraceId", "CorrelationId", "SchemaVersion"]
    .map((name) => `#/components/parameters/${name}`);
  const currentAttemptHeaders = ["X-Request-Id", "X-Trace-Id", "X-Correlation-Id"];
  const expectedStatuses = {
    getProductContentUnit: ["200", "400", "401", "403", "404", "409", "429", "500", "503"],
    getProductSessionWorkspace: ["200", "400", "401", "403", "404", "409", "429", "500", "503"],
    getProductSkillDraft: ["200", "400", "401", "403", "404", "409", "429", "500", "503"],
    upsertProductSkillDraft: ["200", "201", "400", "401", "403", "404", "409", "413", "429", "500", "503"],
    listProductAgentInteractions: ["200", "400", "401", "403", "404", "409", "429", "500", "503"],
    getProductAgentInteraction: ["200", "400", "401", "403", "404", "409", "429", "500", "503"],
    recordProductPatchDecision: ["200", "400", "401", "403", "404", "409", "429", "500", "503"],
  };

  assert.deepEqual(api.security, [{ bearerAuth: [] }]);
  for (const [path, pathItem] of Object.entries(api.paths)) {
    assert.match(path, /^\/product-experience\/v1\//u);
    assert.doesNotMatch(path, /(?:\/worlds\/|skill-builds|skill-versions|activations|\/runs\/)/u);
    for (const [method, operation] of Object.entries(pathItem)) {
      if (!operation?.operationId) continue;
      actual.push(operation.operationId);
      assert.deepEqual(Object.keys(operation.responses).sort(), expectedStatuses[operation.operationId]);
      const parameterRefs = operation.parameters.map((parameter) => parameter.$ref);
      for (const parameter of commonParameters) {
        assert.ok(parameterRefs.includes(parameter), `${operation.operationId} misses ${parameter}`);
      }
      if (["put", "post"].includes(method)) {
        assert.ok(parameterRefs.includes("#/components/parameters/IdempotencyKey"));
        assert.deepEqual(operation["x-idempotency-scope"], [
          "tenant_id", "actor_id", "operationId", "canonical_path", "Idempotency-Key",
        ]);
        assert.equal(operation["x-reconciliation"].method, "GET");
      }
      for (const [status, response] of Object.entries(operation.responses)) {
        const resolved = resolveLocal(api, response);
        for (const header of currentAttemptHeaders) {
          assert.ok(resolved.headers?.[header], `${operation.operationId} HTTP ${status} misses ${header}`);
        }
      }
    }
  }
  assert.deepEqual(actual.sort(), expected);
  assert.equal(api.components.parameters.SchemaVersion.schema.const, "1.0.0");
  assert.equal(api.components.parameters.AfterInteractionSequence.required, true);
  assert.equal(api.components.parameters.AfterInteractionSequence.schema.maximum, Number.MAX_SAFE_INTEGER);
  assert.equal(api.components.parameters.PageLimit.schema.maximum, 100);
  assert.equal(
    api.components.parameters.UnitId.schema.$ref,
    "../schemas/common/content-ref.schema.json#/properties/unit_id",
  );
  assert.equal(
    api.components.parameters.ContentVersion.schema.$ref,
    "../schemas/common/content-ref.schema.json#/properties/version",
  );
  assert.equal(
    api.components.responses.SkillDraftOk.headers.ETag.$ref,
    "#/components/headers/DraftEtag",
  );
  assert.ok(api.components.responses.ContentUnitOk.headers["Cache-Control"]);
  assert.ok(api.components.responses.ContentUnitOk.headers.Vary);
  assert.equal(api.components.responses.Unprocessable, undefined);
  assert.equal(json("contracts/schemas/product-experience/content-unit.schema.json").properties.request_context, undefined);
});

test("Product schemas stay closed and SkillDraft PUT rejects invalid CAS shapes", () => {
  const schemaNames = [
    "content-unit",
    "session-workspace",
    "skill-draft",
    "skill-draft-upsert-request",
    "skill-patch",
    "patch-decision-request",
    "patch-decision-receipt",
    "agent-interaction-source-receipt",
    "agent-interaction",
    "agent-interaction-page",
    "product-write-reconciliation",
  ];
  for (const name of schemaNames) {
    assert.equal(
      json(`contracts/schemas/product-experience/${name}.schema.json`).additionalProperties,
      false,
      `${name} must remain a closed top-level contract`,
    );
  }

  const schemaPath = "contracts/schemas/product-experience/skill-draft-upsert-request.schema.json";
  const update = example("product-skill-draft-upsert-request");
  assertValid(update, schemaPath);

  const createWithHash = structuredClone(update);
  createWithHash.base_revision = 0;
  assertInvalid(createWithHash, schemaPath);

  const updateWithoutHash = structuredClone(update);
  updateWithoutHash.base_draft_sha256 = null;
  assertInvalid(updateWithoutHash, schemaPath);

  const unknownField = structuredClone(update);
  unknownField.expected_revision = 7;
  assertInvalid(unknownField, schemaPath);

  const pathAlias = structuredClone(update);
  pathAlias.source_bundle.entrypoint = "src/./main.cpp";
  pathAlias.source_bundle.files[0].path = "src/./main.cpp";
  assertInvalid(pathAlias, schemaPath);

  const draft = example("product-skill-draft");
  assert.doesNotThrow(() => assertProductSkillDraft(draft));
  const forgedDraftHash = structuredClone(draft);
  forgedDraftHash.draft_sha256 = "f".repeat(64);
  assertSemanticInvalid(() => assertProductSkillDraft(forgedDraftHash));
  const hostPathCollision = structuredClone(draft);
  hostPathCollision.source_bundle.files.push({
    ...hostPathCollision.source_bundle.files[0],
    path: "SRC/main.cpp",
  });
  assertSemanticInvalid(() => assertProductSkillDraft(hostPathCollision));

  const contract = json(schemaPath);
  const invariants = contract["x-invariants"].join("\n");
  assert.match(invariants, /path session_id and draft_id equal the request fields/u);
  assert.match(invariants, /base_revision and base_draft_sha256 both exactly equal/u);
  assert.match(invariants, /same key and byte-equivalent body/u);
  assert.match(invariants, /different body returns 409 IDEMPOTENCY_KEY_REUSED/u);

  const workspace = example("product-session-workspace");
  assert.doesNotThrow(() => assertProductSessionWorkspace(workspace));
  const wrongWorldLink = structuredClone(workspace);
  wrongWorldLink.links.world_snapshot = "/v1/worlds/world_other_001/snapshot";
  assertSemanticInvalid(() => assertProductSessionWorkspace(wrongWorldLink));
  const wrongInteractionHighWatermark = structuredClone(workspace);
  wrongInteractionHighWatermark.last_interaction_sequence = 999;
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGameRelationshipEntries(),
    productEntry("skill-draft", example("product-skill-draft-base")),
    productEntry("skill-draft", example("product-skill-draft")),
    productEntry("session-workspace", wrongInteractionHighWatermark),
    productEntry("agent-interaction-page", example("product-agent-interaction-page")),
  ]));
});

test("Structured SkillPatch and AgentInteraction reject ambiguous or executable patch shapes", () => {
  const patchSchema = "contracts/schemas/product-experience/skill-patch.schema.json";
  const patch = example("product-skill-patch");
  assertValid(patch, patchSchema);
  assert.doesNotThrow(() => assertProductSkillPatch(patch));
  const baseDraft = example("product-skill-draft-base");
  assert.doesNotThrow(() => applyProductSkillPatch(baseDraft, patch));

  const rawWorldMutation = structuredClone(patch);
  rawWorldMutation.operations[0] = { operation: "WORLD_MUTATION", action: "WATER" };
  assertInvalid(rawWorldMutation, patchSchema);

  const traversal = structuredClone(patch);
  traversal.operations[0].path = "../main.cpp";
  assertInvalid(traversal, patchSchema);

  const ambiguousUpsert = structuredClone(patch);
  ambiguousUpsert.operations[0].world_id = "world_demo_001";
  assertInvalid(ambiguousUpsert, patchSchema);

  const forgedContentHash = structuredClone(patch);
  forgedContentHash.operations[0].content += "// forged\n";
  assertSemanticInvalid(() => assertProductSkillPatch(forgedContentHash));

  const canonicalPathCollision = structuredClone(patch);
  canonicalPathCollision.operations.push({
    ...canonicalPathCollision.operations[0],
    path: "SRC/main.cpp",
  });
  assertSemanticInvalid(() => assertProductSkillPatch(canonicalPathCollision));

  const forgedResult = structuredClone(patch);
  forgedResult.result_draft_sha256 = "e".repeat(64);
  forgedResult.patch_sha256 = computeProductPatchSha256(forgedResult);
  assert.doesNotThrow(() => assertProductSkillPatch(forgedResult));
  assertSemanticInvalid(() => applyProductSkillPatch(baseDraft, forgedResult));

  const mislabeledAppliedPatch = structuredClone(example("product-skill-draft"));
  mislabeledAppliedPatch.last_applied_patch_id = "patch_other_001";
  const draftSchemaFile = resolve(
    PROJECT_ROOT,
    "contracts/schemas/product-experience/skill-draft.schema.json",
  );
  const patchSchemaFile = resolve(
    PROJECT_ROOT,
    "contracts/schemas/product-experience/skill-patch.schema.json",
  );
  assertSemanticInvalid(() => assertProductExampleRelationships([
    { schemaFile: draftSchemaFile, value: baseDraft },
    { schemaFile: draftSchemaFile, value: mislabeledAppliedPatch },
    { schemaFile: patchSchemaFile, value: patch },
  ]));

  const interactionSchema = "contracts/schemas/product-experience/agent-interaction.schema.json";
  const pending = example("product-agent-interaction-patch-pending");
  assertValid(pending, interactionSchema);
  assert.doesNotThrow(() => assertProductAgentInteraction(pending));

  const sourceSchema = "contracts/schemas/product-experience/agent-interaction-source-receipt.schema.json";
  const sourceReceipt = example("product-agent-interaction-source-receipt");
  assertValid(sourceReceipt, sourceSchema);
  assert.doesNotThrow(() => assertProductAgentInteractionSourceReceipt(sourceReceipt));
  const tamperedSourceHash = structuredClone(sourceReceipt);
  tamperedSourceHash.actor.actor_id = "student_other_001";
  assertValid(tamperedSourceHash, sourceSchema);
  assertSemanticInvalid(() => assertProductAgentInteractionSourceReceipt(tamperedSourceHash));

  const colludingStructuredProjection = structuredClone(pending);
  colludingStructuredProjection.role = "bug_agent";
  colludingStructuredProjection.response_type = "question";
  colludingStructuredProjection.question = "Which bound should the loop check first?";
  colludingStructuredProjection.hint_level = null;
  colludingStructuredProjection.skill_patch = null;
  colludingStructuredProjection.links.skill_draft = null;
  Object.assign(colludingStructuredProjection.projection_source, {
    role: colludingStructuredProjection.role,
    response_type: colludingStructuredProjection.response_type,
    question: colludingStructuredProjection.question,
    hint_level: colludingStructuredProjection.hint_level,
    skill_patch_sha256: null,
  });
  colludingStructuredProjection.projection_source.source_sha256
    = computeProductInteractionSourceSha256(colludingStructuredProjection.projection_source);
  assertValid(colludingStructuredProjection, interactionSchema);
  assert.doesNotThrow(() => assertProductAgentInteraction(colludingStructuredProjection));
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGameRelationshipEntries(),
    productEntry("agent-interaction", colludingStructuredProjection),
  ]));

  const patchMissing = structuredClone(pending);
  patchMissing.skill_patch = null;
  assertInvalid(patchMissing, interactionSchema);

  const wrongRole = structuredClone(pending);
  wrongRole.role = "world_agent";
  assertInvalid(wrongRole, interactionSchema);

  const nonPatchWithPatch = structuredClone(pending);
  nonPatchWithPatch.response_type = "message";
  nonPatchWithPatch.hint_level = null;
  assertInvalid(nonPatchWithPatch, interactionSchema);

  const illegalHint = structuredClone(pending);
  illegalHint.response_type = "hint";
  illegalHint.skill_patch = null;
  illegalHint.hint_level = 4;
  assertInvalid(illegalHint, interactionSchema);

  const hintWithQuestion = structuredClone(pending);
  hintWithQuestion.response_type = "hint";
  hintWithQuestion.question = "Which loop bound?";
  hintWithQuestion.hint_level = 2;
  hintWithQuestion.skill_patch = null;
  assertInvalid(hintWithQuestion, interactionSchema);

  const forgedFeedback = structuredClone(pending);
  forgedFeedback.feedback.message = "Substituted under the same turn identity.";
  assertSemanticInvalid(() => assertProductAgentInteraction(forgedFeedback));

  const crossSessionFeedback = structuredClone(pending);
  crossSessionFeedback.feedback.session_id = "session_other_001";
  assertSemanticInvalid(() => assertProductAgentInteraction(crossSessionFeedback));

  const interaction = json(interactionSchema);
  const invariants = interaction["x-invariants"].join("\n");
  assert.match(invariants, /feedback\.session_id equals session_id/u);
  assert.match(invariants, /projection_source is byte-equivalent to the canonical receipt/u);
  assert.match(invariants, /skill_patch interaction_id, session_id, turn_id equal this interaction/u);
  assert.match(invariants, /same actor, session and skill/u);
});

test("Product AgentInteraction is identity-closed over canonical Game resources", () => {
  const interaction = example("product-agent-interaction-patch-pending");
  const interactionEntry = productEntry("agent-interaction", interaction);
  const canonicalGame = canonicalGameRelationshipEntries();
  assert.doesNotThrow(() => assertProductExampleRelationships([
    ...canonicalGame,
    interactionEntry,
  ]));
  assertSemanticInvalid(() => assertProductExampleRelationships([interactionEntry]));

  for (const mutateCause of [
    (event) => { event.content_ref.content_hash = "b".repeat(64); },
    (event) => { event.payload.world_revision = 999; },
    (event) => { event.occurred_at = "2026-08-06T10:03:54Z"; },
  ]) {
    const driftedWorldPage = structuredClone(example("game-world-event-page"));
    mutateCause(driftedWorldPage.events[0]);
    assertSemanticInvalid(() => assertProductExampleRelationships([
      ...canonicalGame.filter((entry) => !entry.schemaFile.endsWith("world-event-page.schema.json")),
      gameEntry("world-event-page", driftedWorldPage),
      interactionEntry,
    ]));
  }

  const workspaceEntries = [
    ...canonicalGame,
    productEntry("session-workspace", example("product-session-workspace")),
    productEntry("agent-interaction-page", example("product-agent-interaction-page")),
    productEntry("skill-draft", example("product-skill-draft")),
  ];
  assert.doesNotThrow(() => assertProductExampleRelationships(workspaceEntries));
  const foreignWorldWorkspace = structuredClone(example("product-session-workspace"));
  foreignWorldWorkspace.session.world_id = "world_other_001";
  foreignWorldWorkspace.session.links.world_snapshot = "/v1/worlds/world_other_001/snapshot";
  foreignWorldWorkspace.world_checkpoint.world_id = "world_other_001";
  foreignWorldWorkspace.links.world_snapshot = "/v1/worlds/world_other_001/snapshot";
  assert.doesNotThrow(() => assertProductSessionWorkspace(foreignWorldWorkspace));
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...workspaceEntries.filter((entry) => !entry.schemaFile.endsWith("session-workspace.schema.json")),
    productEntry("session-workspace", foreignWorldWorkspace),
  ]));

  const missingSourceEvent = canonicalGame.filter((entry) => (
    !entry.schemaFile.endsWith("agent-turn-feedback-ready-event.schema.json")
  ));
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...missingSourceEvent,
    interactionEntry,
  ]));

  const forgedProjection = structuredClone(interaction);
  forgedProjection.feedback.message = "Shape-valid feedback substituted under canonical IDs.";
  forgedProjection.feedback_event.feedback_sha256 = computeProductFeedbackSha256(
    forgedProjection.feedback,
  );
  rebindProjectionSourceFeedback(forgedProjection);
  assert.doesNotThrow(() => assertProductAgentInteraction(forgedProjection));
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGame,
    productEntry("agent-interaction", forgedProjection),
  ]));

  const forgedEvent = structuredClone(example("product-agent-feedback-source-event"));
  forgedEvent.payload.message = "Runtime event and Product projection colluded.";
  const projectionMatchingForgedEvent = structuredClone(interaction);
  projectionMatchingForgedEvent.feedback = structuredClone(forgedEvent.payload);
  projectionMatchingForgedEvent.feedback_event.feedback_sha256 = computeProductFeedbackSha256(
    projectionMatchingForgedEvent.feedback,
  );
  rebindProjectionSourceFeedback(projectionMatchingForgedEvent);
  assert.doesNotThrow(() => assertProductAgentInteraction(projectionMatchingForgedEvent));
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGame.filter((entry) => (
      !entry.schemaFile.endsWith("agent-turn-feedback-ready-event.schema.json")
    )),
    gameEntry("agent-turn-feedback-ready-event", forgedEvent),
    productEntry("agent-interaction", projectionMatchingForgedEvent),
  ]));

  const wrongStreamEvent = structuredClone(example("product-agent-feedback-source-event"));
  wrongStreamEvent.stream_id = "agent-session:session_other_001";
  const projectionMatchingWrongStream = structuredClone(interaction);
  projectionMatchingWrongStream.feedback_event.stream_id = wrongStreamEvent.stream_id;
  assert.doesNotThrow(() => assertProductAgentInteraction(projectionMatchingWrongStream));
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGame.filter((entry) => (
      !entry.schemaFile.endsWith("agent-turn-feedback-ready-event.schema.json")
    )),
    gameEntry("agent-turn-feedback-ready-event", wrongStreamEvent),
    productEntry("agent-interaction", projectionMatchingWrongStream),
  ]));

  const foreignSession = structuredClone(example("game-agent-session"));
  foreignSession.request_context.actor.actor_id = "student_other_001";
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGame.filter((entry) => !entry.schemaFile.endsWith("agent-session.schema.json")),
    gameEntry("agent-session", foreignSession),
    interactionEntry,
  ]));

  for (const mutateSessionLink of [
    (session) => { session.links.self = "/v1/agent-sessions/session_other_001"; },
    (session) => { session.links.turns = "/v1/agent-sessions/session_other_001/turns"; },
    (session) => { session.links.world_snapshot = "/v1/worlds/world_other_001/snapshot"; },
  ]) {
    const wrongSessionLink = structuredClone(example("game-agent-session"));
    mutateSessionLink(wrongSessionLink);
    assertSemanticInvalid(() => assertProductExampleRelationships([
      ...canonicalGame.filter((entry) => !entry.schemaFile.endsWith("agent-session.schema.json")),
      gameEntry("agent-session", wrongSessionLink),
      interactionEntry,
    ]));
  }

  const foreignLearnerEvidence = structuredClone(example("game-evidence"));
  foreignLearnerEvidence.subject.learner_id = "learner_other_001";
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGame.filter((entry) => !entry.schemaFile.endsWith("evidence.schema.json")),
    gameEntry("evidence", foreignLearnerEvidence),
    interactionEntry,
  ]));

  const foreignWorldSession = structuredClone(example("game-agent-session"));
  foreignWorldSession.world_id = "world_other_001";
  foreignWorldSession.links.world_snapshot = "/v1/worlds/world_other_001/snapshot";
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGame.filter((entry) => !entry.schemaFile.endsWith("agent-session.schema.json")),
    gameEntry("agent-session", foreignWorldSession),
    interactionEntry,
  ]));

  const wrongCommandType = structuredClone(example("game-command"));
  wrongCommandType.command_type = "CREATE_AGENT_SESSION";
  wrongCommandType.result = {
    result_type: "RESOURCE_CREATED",
    resource_type: "AGENT_SESSION",
    resource_id: "session_agent_001",
    resource_url: "/v1/agent-sessions/session_agent_001",
  };
  assertValid(wrongCommandType, "contracts/schemas/game/command.schema.json");
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGame.filter((entry) => !entry.schemaFile.endsWith("command.schema.json")),
    gameEntry("command", wrongCommandType),
    interactionEntry,
  ]));

  const cancelledCommand = structuredClone(example("game-command"));
  cancelledCommand.status = "CANCELLED";
  cancelledCommand.result = null;
  assertValid(cancelledCommand, "contracts/schemas/game/command.schema.json");
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGame.filter((entry) => !entry.schemaFile.endsWith("command.schema.json")),
    gameEntry("command", cancelledCommand),
    interactionEntry,
  ]));

  const noEffectCommand = structuredClone(example("game-command"));
  noEffectCommand.result = { result_type: "NO_EFFECT", reason_code: "NO_EFFECT" };
  assertValid(noEffectCommand, "contracts/schemas/game/command.schema.json");
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...canonicalGame.filter((entry) => !entry.schemaFile.endsWith("command.schema.json")),
    gameEntry("command", noEffectCommand),
    interactionEntry,
  ]));

  const forgedEvidenceReference = {
    ...interaction.feedback.evidence_refs[0],
    evidence_type: "ACTION_LOG",
    sha256: "d".repeat(64),
  };
  const colludingProjection = structuredClone(interaction);
  colludingProjection.feedback.evidence_refs = [forgedEvidenceReference];
  colludingProjection.skill_patch.evidence_refs = [forgedEvidenceReference];
  colludingProjection.feedback_event.feedback_sha256 = computeProductFeedbackSha256(
    colludingProjection.feedback,
  );
  const colludingEvent = structuredClone(example("product-agent-feedback-source-event"));
  colludingEvent.payload = structuredClone(colludingProjection.feedback);
  const colludingRun = structuredClone(example("game-run"));
  colludingRun.agent_feedback = structuredClone(colludingProjection.feedback);
  colludingRun.evidence_refs = [forgedEvidenceReference];
  const colludingCommand = structuredClone(example("game-command"));
  colludingCommand.evidence_refs = [forgedEvidenceReference];
  const colludingEvidence = structuredClone(example("game-evidence"));
  colludingEvidence.evidence_ref = forgedEvidenceReference;
  colludingEvidence.source.source_type = "SKILL_RUN";
  colludingEvidence.source.source_id = "run_water_0001";
  colludingEvidence.payload = {
    evidence_kind: "SKILL_RUN",
    run_id: "run_water_0001",
    sandbox_status: "SUCCEEDED",
    world_status: "COMMITTED",
    intent_count: 1,
  };
  assertSemanticInvalid(() => assertProductExampleRelationships([
    gameEntry("agent-session", example("game-agent-session")),
    gameEntry("command", colludingCommand),
    gameEntry("run", colludingRun),
    gameEntry("evidence", colludingEvidence),
    gameEntry("agent-turn-feedback-ready-event", colludingEvent),
    gameEntry("world-event-page", example("game-world-event-page")),
    productEntry("agent-interaction", colludingProjection),
  ]));

  const wrongOwnerPayload = {
    evidence_kind: "SKILL_RUN",
    run_id: "run_other_0001",
    sandbox_status: "SUCCEEDED",
    world_status: "COMMITTED",
    intent_count: 1,
  };
  const wrongOwnerHash = canonicalJsonSha256V1(wrongOwnerPayload);
  const wrongOwnerReference = {
    ...forgedEvidenceReference,
    sha256: wrongOwnerHash,
  };
  const wrongOwnerProjection = structuredClone(interaction);
  wrongOwnerProjection.feedback.evidence_refs = [wrongOwnerReference];
  wrongOwnerProjection.skill_patch.evidence_refs = [wrongOwnerReference];
  wrongOwnerProjection.feedback_event.feedback_sha256 = computeProductFeedbackSha256(
    wrongOwnerProjection.feedback,
  );
  const wrongOwnerEvent = structuredClone(example("product-agent-feedback-source-event"));
  wrongOwnerEvent.payload = structuredClone(wrongOwnerProjection.feedback);
  const wrongOwnerRun = structuredClone(example("game-run"));
  wrongOwnerRun.agent_feedback = structuredClone(wrongOwnerProjection.feedback);
  wrongOwnerRun.evidence_refs = [wrongOwnerReference];
  const wrongOwnerCommand = structuredClone(example("game-command"));
  wrongOwnerCommand.evidence_refs = [wrongOwnerReference];
  const wrongOwnerEvidence = structuredClone(example("game-evidence"));
  wrongOwnerEvidence.evidence_ref = wrongOwnerReference;
  wrongOwnerEvidence.source.source_type = "SKILL_RUN";
  wrongOwnerEvidence.source.source_id = wrongOwnerPayload.run_id;
  wrongOwnerEvidence.payload = wrongOwnerPayload;
  wrongOwnerEvidence.integrity.payload_sha256 = wrongOwnerHash;
  assertSemanticInvalid(() => assertProductExampleRelationships([
    gameEntry("agent-session", example("game-agent-session")),
    gameEntry("command", wrongOwnerCommand),
    gameEntry("run", wrongOwnerRun),
    gameEntry("evidence", wrongOwnerEvidence),
    gameEntry("agent-turn-feedback-ready-event", wrongOwnerEvent),
    gameEntry("world-event-page", example("game-world-event-page")),
    productEntry("agent-interaction", wrongOwnerProjection),
  ]));

  const noRunProjection = structuredClone(interaction);
  noRunProjection.feedback.run_id = null;
  noRunProjection.feedback_event.feedback_sha256 = computeProductFeedbackSha256(
    noRunProjection.feedback,
  );
  const noRunEvent = structuredClone(example("product-agent-feedback-source-event"));
  noRunEvent.payload = structuredClone(noRunProjection.feedback);
  const noRunCommand = structuredClone(example("game-command"));
  noRunCommand.result = { result_type: "NO_EFFECT", reason_code: "NO_EFFECT" };
  delete noRunCommand.links.run;
  delete noRunCommand.links.world_snapshot;
  assertSemanticInvalid(() => assertProductExampleRelationships([
    gameEntry("agent-session", example("game-agent-session")),
    gameEntry("command", noRunCommand),
    gameEntry("evidence", example("game-evidence")),
    gameEntry("agent-turn-feedback-ready-event", noRunEvent),
    gameEntry("world-event-page", example("game-world-event-page")),
    productEntry("agent-interaction", noRunProjection),
  ]));
});

test("Patch decisions, pagination and durable-write reconciliation reject unsafe states", () => {
  const requestSchema = "contracts/schemas/product-experience/patch-decision-request.schema.json";
  const request = example("product-patch-decision-request");
  assertValid(request, requestSchema);

  const acceptWithReason = structuredClone(request);
  acceptWithReason.reason_code = "STUDENT_REJECTED";
  assertInvalid(acceptWithReason, requestSchema);

  const rejectWithoutReason = structuredClone(request);
  rejectWithoutReason.decision = "REJECT";
  assertInvalid(rejectWithoutReason, requestSchema);

  const wrongPatchAlias = structuredClone(request);
  wrongPatchAlias.patch_hash = wrongPatchAlias.patch_sha256;
  assertInvalid(wrongPatchAlias, requestSchema);

  const receiptSchema = "contracts/schemas/product-experience/patch-decision-receipt.schema.json";
  const receipt = example("product-patch-decision-response");
  assertValid(receipt, receiptSchema);
  assert.doesNotThrow(() => assertProductPatchDecisionReceipt(receipt, example("product-skill-patch")));
  const acceptWithoutDraftMutation = structuredClone(receipt);
  acceptWithoutDraftMutation.draft_updated = false;
  assertInvalid(acceptWithoutDraftMutation, receiptSchema);

  const rejectRequest = example("product-patch-decision-reject-request");
  const rejectPatch = example("product-skill-patch-reject");
  const rejectPending = example("product-agent-interaction-patch-reject-pending");
  const rejectedInteraction = example("product-agent-interaction-patch-rejected");
  const rejectReceipt = example("product-patch-decision-reject-response");
  assertValid(rejectRequest, requestSchema);
  assert.doesNotThrow(() => assertProductPatchDecisionRequest(
    rejectRequest,
    { interaction: rejectPending, patch: rejectPatch },
  ));
  const rejectMissingReason = structuredClone(rejectRequest);
  delete rejectMissingReason.reason_code;
  assertInvalid(rejectMissingReason, requestSchema);
  const rejectNullReason = structuredClone(rejectRequest);
  rejectNullReason.reason_code = null;
  assertInvalid(rejectNullReason, requestSchema);
  assertValid(rejectReceipt, receiptSchema);
  assert.doesNotThrow(() => assertProductPatchDecisionReceipt(
    rejectReceipt,
    rejectPatch,
    rejectRequest,
  ));
  assert.equal(rejectReceipt.draft_updated, false);
  assert.equal(rejectReceipt.draft_revision_after, rejectReceipt.draft_revision_before);
  assert.equal(rejectReceipt.draft_sha256_after, rejectReceipt.draft_sha256_before);
  assert.equal(
    rejectReceipt.interaction_revision_after,
    rejectReceipt.interaction_revision_before + 1,
  );
  assertValid(
    rejectedInteraction,
    "contracts/schemas/product-experience/agent-interaction.schema.json",
  );
  assert.doesNotThrow(() => assertProductAgentInteraction(rejectedInteraction));
  const frozenRejectRelationshipInputs = [
    ...canonicalRejectRelationshipEntries(),
    productEntry("skill-draft", example("product-skill-draft-base")),
    productEntry("skill-patch", rejectPatch),
    productEntry("patch-decision-request", rejectRequest),
    productEntry("patch-decision-receipt", rejectReceipt),
    productEntry("agent-interaction", rejectPending),
    productEntry("agent-interaction", rejectedInteraction),
  ];
  assert.doesNotThrow(() => assertProductExampleRelationships(frozenRejectRelationshipInputs));

  const rejectThatMutatedDraft = structuredClone(rejectReceipt);
  rejectThatMutatedDraft.draft_revision_after += 1;
  rejectThatMutatedDraft.draft_sha256_after = rejectPatch.result_draft_sha256;
  assertSemanticInvalid(() => assertProductPatchDecisionReceipt(
    rejectThatMutatedDraft,
    rejectPatch,
    rejectRequest,
  ));
  const rejectWithoutInteractionAdvance = structuredClone(rejectedInteraction);
  rejectWithoutInteractionAdvance.interaction_revision = 1;
  assertSemanticInvalid(() => assertProductAgentInteraction(rejectWithoutInteractionAdvance));
  const rejectWithForgedPatchIdentity = structuredClone(rejectRequest);
  rejectWithForgedPatchIdentity.patch_sha256 = "f".repeat(64);
  assertSemanticInvalid(() => assertProductPatchDecisionRequest(
    rejectWithForgedPatchIdentity,
    { interaction: rejectPending, patch: rejectPatch },
  ));
  const rejectedWithForgedSource = structuredClone(rejectedInteraction);
  rejectedWithForgedSource.projection_source.skill_patch_sha256 = "f".repeat(64);
  rejectedWithForgedSource.projection_source.source_sha256
    = computeProductInteractionSourceSha256(rejectedWithForgedSource.projection_source);
  assertSemanticInvalid(() => assertProductAgentInteraction(rejectedWithForgedSource));

  const frozenRelationshipInputs = [
    ...canonicalGameRelationshipEntries(),
    productEntry("skill-draft", example("product-skill-draft-base")),
    productEntry("skill-draft", example("product-skill-draft")),
    productEntry("skill-patch", example("product-skill-patch")),
    productEntry("patch-decision-request", request),
    productEntry("agent-interaction", example("product-agent-interaction-patch-pending")),
    productEntry("agent-interaction", example("product-agent-interaction-patch-accepted")),
  ];
  const receiptForAnotherDecision = structuredClone(receipt);
  receiptForAnotherDecision.decision_id = "decision_other_001";
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...frozenRelationshipInputs,
    productEntry("patch-decision-receipt", receiptForAnotherDecision),
  ]));

  const interactionWithDriftedFeedback = structuredClone(
    example("product-agent-interaction-patch-accepted"),
  );
  interactionWithDriftedFeedback.feedback.message = "A different message at revision two.";
  interactionWithDriftedFeedback.feedback_event.feedback_sha256 = computeProductFeedbackSha256(
    interactionWithDriftedFeedback.feedback,
  );
  rebindProjectionSourceFeedback(interactionWithDriftedFeedback);
  assert.doesNotThrow(() => assertProductAgentInteraction(interactionWithDriftedFeedback));
  assertSemanticInvalid(() => assertProductExampleRelationships([
    ...frozenRelationshipInputs.filter((entry) => (
      !(entry.schemaFile.endsWith("agent-interaction.schema.json")
        && entry.value.interaction_revision === 2)
    )),
    productEntry("agent-interaction", interactionWithDriftedFeedback),
    productEntry("patch-decision-receipt", receipt),
  ]));

  const rejectThatAdvancedDraft = structuredClone(receipt);
  rejectThatAdvancedDraft.decision = "REJECT";
  rejectThatAdvancedDraft.reason_code = "STUDENT_REJECTED";
  rejectThatAdvancedDraft.draft_updated = false;
  assertSemanticInvalid(() => assertProductPatchDecisionReceipt(
    rejectThatAdvancedDraft,
    example("product-skill-patch"),
  ));

  const pageSchema = "contracts/schemas/product-experience/agent-interaction-page.schema.json";
  const page = example("product-agent-interaction-page");
  assertValid(page, pageSchema);
  assert.doesNotThrow(() => assertProductAgentInteractionPage(page, 0));
  const emptyPageWithAdvancedBounds = structuredClone(page);
  emptyPageWithAdvancedBounds.interactions = [];
  assertInvalid(emptyPageWithAdvancedBounds, pageSchema);

  const emptyPageThatAdvances = structuredClone(page);
  emptyPageThatAdvances.interactions = [];
  emptyPageThatAdvances.from_sequence = null;
  emptyPageThatAdvances.to_sequence = null;
  emptyPageThatAdvances.has_more = false;
  assertValid(emptyPageThatAdvances, pageSchema);
  assertSemanticInvalid(() => assertProductAgentInteractionPage(emptyPageThatAdvances, 0));

  const gap = structuredClone(page);
  gap.interactions[0].sequence = 2;
  gap.from_sequence = 2;
  gap.to_sequence = 2;
  gap.next_after_sequence = 2;
  gap.high_watermark_sequence = 2;
  assertSemanticInvalid(() => assertProductAgentInteractionPage(gap, 0));

  const crossActor = structuredClone(page);
  crossActor.interactions[0].request_context.actor.actor_id = "student_other_001";
  assertSemanticInvalid(() => assertProductAgentInteractionPage(crossActor, 0));

  const reconciliationSchema = "contracts/schemas/product-experience/product-write-reconciliation.schema.json";
  const reconciliation = example("product-write-reconciliation");
  assertValid(reconciliation, reconciliationSchema);
  assert.doesNotThrow(() => assertProductWriteReconciliation(reconciliation));
  const notDurable = structuredClone(reconciliation);
  notDurable.error.details.operation_was_durably_accepted = false;
  assertInvalid(notDurable, reconciliationSchema);
  const worldCommitAlias = structuredClone(reconciliation);
  worldCommitAlias.error.code = "UNKNOWN_COMMIT_STATE";
  assertInvalid(worldCommitAlias, reconciliationSchema);
  const leakedDetail = structuredClone(reconciliation);
  leakedDetail.error.details.secret = "student-source";
  assertInvalid(leakedDetail, reconciliationSchema);
  const wrongResourcePair = structuredClone(reconciliation);
  wrongResourcePair.reconciliation.resource_type = "SKILL_DRAFT";
  assertSemanticInvalid(() => assertProductWriteReconciliation(wrongResourcePair));
  const wrongResourceIdentity = structuredClone(reconciliation);
  wrongResourceIdentity.reconciliation.resource_id = "interaction_other_001";
  assertSemanticInvalid(() => assertProductWriteReconciliation(wrongResourceIdentity));
  const wrongSessionRoute = structuredClone(reconciliation);
  wrongSessionRoute.reconciliation.resource_url = "/product-experience/v1/sessions/session_other_001/agent-interactions/interaction_water_001";
  assertSemanticInvalid(() => assertProductWriteReconciliation(wrongSessionRoute));

  const decisionInvariants = json(requestSchema)["x-invariants"].join("\n");
  assert.match(decisionInvariants, /ACCEPT atomically/u);
  assert.match(decisionInvariants, /REJECT atomically/u);
  assert.match(decisionInvariants, /failure writes nothing/u);
  const pageInvariants = json(pageSchema)["x-invariants"].join("\n");
  assert.match(pageInvariants, /strictly ordered and gap-free/u);
  assert.match(pageInvariants, /empty page.*does not advance|empty page.*next_after_sequence equals requested_after_sequence/u);
});

test("Product error bodies exclude unrelated Game, World, Sandbox and Feishu codes", () => {
  const contract = schema(
    "contracts/schemas/product-experience/product-error-responses-by-status.schema.json",
  );
  const authenticationRequired = {
    request_id: "req_product_error_0001",
    trace_id: "trace_product_error_0001",
    status: "REJECTED",
    data: null,
    error: {
      code: "AUTHENTICATION_REQUIRED",
      category: "AUTHENTICATION",
      retryable: false,
      user_message_key: "auth.login_required",
      stage: "AUTHENTICATE",
    },
  };
  assert.doesNotThrow(() => assertSchema(
    authenticationRequired,
    contract.value.$defs.status401,
    contract.absolutePath,
    documents,
  ));

  const feishuSignature = structuredClone(authenticationRequired);
  feishuSignature.error = {
    code: "FEISHU_SIGNATURE_INVALID",
    category: "AUTHENTICATION",
    retryable: false,
    user_message_key: "feishu.signature_invalid",
    stage: "AUTHENTICATE",
  };
  assert.throws(() => assertSchema(
    feishuSignature,
    contract.value.$defs.status401,
    contract.absolutePath,
    documents,
  ));

  assert.deepEqual(contract.value.$defs.error409.properties.code.enum, [
    "SCHEMA_VERSION_UNSUPPORTED",
    "CONTENT_VERSION_MISMATCH",
    "IDEMPOTENCY_KEY_REUSED",
  ]);
  assert.deepEqual(contract.value.$defs.error500.properties.code.enum, [
    "INVARIANT_VIOLATION",
    "INTERNAL_ERROR",
  ]);
  assert.equal(contract.value.$defs.error503.properties.code.const, "DEPENDENCY_UNAVAILABLE");
});
