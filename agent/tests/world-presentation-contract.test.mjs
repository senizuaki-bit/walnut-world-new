import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { canonicalJsonSha256V1 } from "../src/canonical-json.mjs";
import {
  assertSchema,
  loadDocuments,
  PROJECT_ROOT,
} from "../scripts/validate-contracts.mjs";

const EVENT_SCHEMA_PATH = resolve(
  PROJECT_ROOT,
  "contracts/schemas/game/world-presentation-event.schema.json",
);
const PAGE_SCHEMA_PATH = resolve(
  PROJECT_ROOT,
  "contracts/schemas/game/world-presentation-event-page.schema.json",
);
const EXAMPLE_PATH = resolve(
  PROJECT_ROOT,
  "contracts/examples/game-world-presentation-event-page.json",
);
const OPENAPI_PATH = resolve(
  PROJECT_ROOT,
  "contracts/openapi/int2-world-presentation.openapi.json",
);

const EVENT_FIELDS = [
  "event_id", "event_type", "event_version", "schema_version", "stream_id", "sequence",
  "occurred_at", "producer", "tenant_id", "session_id", "turn_id", "command_id", "run_id",
  "world_id", "commit_id", "world_revision", "action_index", "action_count", "intent_id",
  "state_hash_before", "state_hash_after", "final_snapshot_revision",
  "final_world_event_sequence", "final_snapshot_state_hash", "payload", "payload_sha256",
  "integrity_sha256",
].sort();
const PAYLOAD_FIELDS = [
  "actor_entity_id", "plot_id", "position", "crop_type", "growth_stage",
  "ready_to_harvest",
].sort();
const PAGE_FIELDS = [
  "request_context", "world_id", "snapshot_revision", "snapshot_last_event_sequence",
  "snapshot_state_hash", "presentation_high_watermark", "from_sequence", "to_sequence",
  "has_more", "next_after_sequence", "events",
].sort();

function json(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function integrityVector(event) {
  return [
    event.event_type,
    event.event_version,
    event.schema_version,
    event.stream_id,
    event.sequence,
    event.occurred_at,
    event.producer,
    event.tenant_id,
    event.session_id,
    event.turn_id,
    event.command_id,
    event.run_id,
    event.world_id,
    event.commit_id,
    event.world_revision,
    event.action_index,
    event.action_count,
    event.intent_id,
    event.state_hash_before,
    event.state_hash_after,
    event.final_snapshot_revision,
    event.final_world_event_sequence,
    event.final_snapshot_state_hash,
    event.payload_sha256,
    event.payload.actor_entity_id,
    event.payload.plot_id,
    event.payload.position.x,
    event.payload.position.y,
    event.payload.crop_type,
    event.payload.growth_stage,
    event.payload.ready_to_harvest,
  ];
}

function integritySha256(event) {
  return createHash("sha256").update(JSON.stringify(integrityVector(event)), "utf8").digest("hex");
}

function sealEvent(event) {
  event.payload_sha256 = canonicalJsonSha256V1(event.payload);
  event.integrity_sha256 = integritySha256(event);
  event.event_id = `presentation_${event.integrity_sha256.slice(0, 32)}`;
  return event;
}

function assertPresentationPage(page, requestAfterSequence = undefined) {
  if (page.events.length === 0) {
    assert.ok(Number.isInteger(requestAfterSequence), "empty page requires request after_sequence");
    assert.equal(page.from_sequence, requestAfterSequence, "empty from_sequence mismatch");
    assert.equal(page.to_sequence, requestAfterSequence, "empty to_sequence mismatch");
    assert.equal(page.next_after_sequence, requestAfterSequence, "empty next_after_sequence mismatch");
    assert.equal(page.has_more, false, "empty page has_more must be false");
    return;
  }
  assert.equal(page.from_sequence, page.events[0].sequence, "from_sequence mismatch");
  assert.equal(page.to_sequence, page.events.at(-1).sequence, "to_sequence mismatch");
  assert.equal(page.next_after_sequence, page.to_sequence, "next_after_sequence mismatch");
  assert.ok(page.presentation_high_watermark >= page.to_sequence, "presentation_high_watermark mismatch");
  assert.equal(
    page.has_more,
    page.to_sequence < page.presentation_high_watermark,
    "has_more/high-watermark mismatch",
  );
  assert.equal(
    new Set(page.events.map((event) => event.event_id)).size,
    page.events.length,
    "duplicate event_id",
  );

  page.events.forEach((event, index) => {
    assert.equal(event.sequence, page.from_sequence + index, "global sequence must be 1-based and gap-free");
    assert.ok(event.sequence >= 1, "global sequence is 1-based");
    assert.ok(
      event.action_index >= 0 && event.action_index < event.action_count,
      "0-based action_index is outside action_count",
    );
    assert.equal(event.event_type, "world.action.harvested", "event_type must be HARVEST-only");
    assert.equal(event.stream_id, `world-presentation:${page.world_id}`, "stream_id mismatch");
    assert.equal(event.world_id, page.world_id, "world_id mismatch");
    assert.equal(event.tenant_id, page.request_context.actor.tenant_id, "tenant_id mismatch");
    assert.equal(event.world_revision, event.final_snapshot_revision, "world_revision mismatch");
    assert.ok(event.final_snapshot_revision <= page.snapshot_revision, "event revision exceeds page snapshot head");
    assert.ok(
      event.final_world_event_sequence <= page.snapshot_last_event_sequence,
      "event world sequence exceeds page snapshot head",
    );
    assert.equal(event.payload.ready_to_harvest, true, "ready_to_harvest mismatch");
    assert.equal(event.payload_sha256, canonicalJsonSha256V1(event.payload), "payload_sha256 mismatch");
    assert.equal(event.integrity_sha256, integritySha256(event), "integrity_sha256 mismatch");
    assert.equal(
      event.event_id,
      `presentation_${event.integrity_sha256.slice(0, 32)}`,
      "event_id mismatch",
    );
    if (index > 0) {
      const previous = page.events[index - 1];
      if (event.commit_id === previous.commit_id) {
        for (const field of [
          "tenant_id", "session_id", "turn_id", "command_id", "run_id", "world_id",
          "world_revision", "action_count", "final_snapshot_revision",
          "final_world_event_sequence", "final_snapshot_state_hash",
        ]) {
          assert.equal(event[field], previous[field], `same commit ${field} mismatch`);
        }
        assert.equal(event.action_index, previous.action_index + 1, "same commit action_index gap");
        assert.equal(event.state_hash_before, previous.state_hash_after, "same commit state hash gap");
      } else {
        assert.equal(
          previous.action_index,
          previous.action_count - 1,
          "commit boundary precedes an unfinished action set",
        );
        assert.equal(event.action_index, 0, "new commit must start at action_index zero");
        assert.ok(
          event.final_snapshot_revision > previous.final_snapshot_revision,
          "commit-final revision must increase",
        );
        assert.ok(
          event.final_world_event_sequence > previous.final_world_event_sequence,
          "commit-final world event sequence must increase",
        );
      }
    }
    if (event.action_index === event.action_count - 1) {
      assert.equal(
        event.state_hash_after,
        event.final_snapshot_state_hash,
        "closed action set does not reach its commit-final state hash",
      );
    }
  });
  if (page.to_sequence === page.presentation_high_watermark) {
    const last = page.events.at(-1);
    assert.equal(last.action_index, last.action_count - 1, "high-watermark tail is not closed");
    assert.equal(last.final_snapshot_revision, page.snapshot_revision, "head snapshot revision mismatch");
    assert.equal(
      last.final_world_event_sequence,
      page.snapshot_last_event_sequence,
      "head snapshot world event sequence mismatch",
    );
    assert.equal(last.final_snapshot_state_hash, page.snapshot_state_hash, "head snapshot state hash mismatch");
  }
}

function expectPageRejected(page, pattern) {
  assert.throws(() => assertPresentationPage(page), pattern);
}

test("v0.5 world presentation schemas are closed, HARVEST-only, and exact", () => {
  const eventSchema = json(EVENT_SCHEMA_PATH);
  const pageSchema = json(PAGE_SCHEMA_PATH);
  assert.equal(eventSchema.additionalProperties, false);
  assert.deepEqual([...eventSchema.required].sort(), EVENT_FIELDS);
  assert.deepEqual(Object.keys(eventSchema.properties).sort(), EVENT_FIELDS);
  assert.equal(eventSchema.properties.event_type.const, "world.action.harvested");
  assert.equal(eventSchema.properties.event_version.const, 1);
  assert.equal(eventSchema.properties.schema_version.const, "1.0.0");
  assert.equal(eventSchema.properties.producer.const, "walnut_world_engine");
  assert.equal(eventSchema.properties.sequence.minimum, 1);
  assert.equal(eventSchema.properties.action_index.minimum, 0);
  assert.equal(eventSchema.properties.payload.additionalProperties, false);
  assert.deepEqual([...eventSchema.properties.payload.required].sort(), PAYLOAD_FIELDS);
  assert.deepEqual(Object.keys(eventSchema.properties.payload.properties).sort(), PAYLOAD_FIELDS);
  assert.equal(eventSchema.properties.payload.properties.ready_to_harvest.const, true);
  assert.match(eventSchema["x-invariants"].join("\n"), /0 <= action_index < action_count/u);
  assert.match(eventSchema["x-invariants"].join("\n"), /global sequence is 1-based/u);

  assert.equal(pageSchema.additionalProperties, false);
  assert.deepEqual([...pageSchema.required].sort(), PAGE_FIELDS);
  assert.deepEqual(Object.keys(pageSchema.properties).sort(), PAGE_FIELDS);
  assert.equal(pageSchema.properties.events.items.$ref, "./world-presentation-event.schema.json");
  assert.match(pageSchema["x-invariants"].join("\n"), /empty page.*request after_sequence/u);
  assert.match(pageSchema["x-invariants"].join("\n"), /historical events retain their own commit-final state hash/u);
  assert.match(pageSchema["x-invariants"].join("\n"), /At a commit boundary/u);
  assert.match(pageSchema["x-invariants"].join("\n"), /reaches presentation_high_watermark/u);
});

test("released presentation page validates and is bound byte-for-byte to payload and final snapshot", () => {
  const { documents } = loadDocuments();
  const pageSchema = documents.get(PAGE_SCHEMA_PATH);
  const example = json(EXAMPLE_PATH);
  assert.equal(example.schema_ref, "../schemas/game/world-presentation-event-page.schema.json");
  assertSchema(example.value, pageSchema, PAGE_SCHEMA_PATH, documents);
  assertPresentationPage(example.value);
});

test("pagination across two commits retains historical bindings and closes commit boundaries", () => {
  const firstCommitPage = json(EXAMPLE_PATH).value;
  const secondCommit = firstCommitPage.events.map((source, index) => {
    const event = clone(source);
    event.sequence = 3 + index;
    event.occurred_at = "2026-08-14T10:03:54Z";
    event.turn_id = "turn_demo_0002";
    event.command_id = "cmd_game_00000002";
    event.run_id = "run_game_00000002";
    event.commit_id = "commit_world_00000002";
    event.world_revision = 187;
    event.action_index = index;
    event.intent_id = `intent_harvest_000${3 + index}`;
    event.state_hash_before = (index === 0 ? "3" : "4").repeat(64);
    event.state_hash_after = (index === 0 ? "4" : "5").repeat(64);
    event.final_snapshot_revision = 187;
    event.final_world_event_sequence = 749;
    event.final_snapshot_state_hash = "5".repeat(64);
    event.payload.plot_id = `farm_plot_000${3 + index}`;
    event.payload.position.x = 14 + index;
    return sealEvent(event);
  });
  const head = {
    ...clone(firstCommitPage),
    snapshot_revision: 187,
    snapshot_last_event_sequence: 749,
    snapshot_state_hash: "5".repeat(64),
    presentation_high_watermark: 4,
    from_sequence: 1,
    to_sequence: 4,
    has_more: false,
    next_after_sequence: 4,
    events: [...clone(firstCommitPage.events), ...secondCommit],
  };
  assertPresentationPage(head);

  const crossCommitPage = {
    ...clone(head),
    from_sequence: 2,
    to_sequence: 3,
    has_more: true,
    next_after_sequence: 3,
    events: [clone(head.events[1]), clone(head.events[2])],
  };
  assert.notEqual(
    crossCommitPage.events[0].final_snapshot_state_hash,
    crossCommitPage.snapshot_state_hash,
    "the fixture must exercise a historical commit binding",
  );
  assertPresentationPage(crossCommitPage);

  const unfinishedBoundary = clone(crossCommitPage);
  unfinishedBoundary.events[0].action_index = 0;
  sealEvent(unfinishedBoundary.events[0]);
  expectPageRejected(unfinishedBoundary, /commit boundary.*unfinished/u);

  const falseSameCommit = clone(crossCommitPage);
  falseSameCommit.events[1].commit_id = falseSameCommit.events[0].commit_id;
  sealEvent(falseSameCommit.events[1]);
  expectPageRejected(falseSameCommit, /same commit/u);

  const openHighWatermarkTail = clone(head);
  openHighWatermarkTail.events.at(-1).action_index = 0;
  sealEvent(openHighWatermarkTail.events.at(-1));
  expectPageRejected(openHighWatermarkTail, /action_index|high-watermark tail/u);
});

test("presentation corruption is rejected closed instead of advancing to pseudo-success", async (context) => {
  const example = json(EXAMPLE_PATH).value;

  await context.test("duplicate, gap and reordering", () => {
    const duplicate = clone(example);
    duplicate.events[1] = clone(duplicate.events[0]);
    expectPageRejected(duplicate, /sequence|event_id/u);

    const gap = clone(example);
    gap.events[1].sequence += 1;
    expectPageRejected(gap, /sequence/u);

    const reordered = clone(example);
    [reordered.events[0], reordered.events[1]] = [reordered.events[1], reordered.events[0]];
    expectPageRejected(reordered, /sequence|action_index/u);
  });

  await context.test("unknown type and payload tampering", () => {
    const unknown = clone(example);
    unknown.events[0].event_type = "world.action.watered";
    expectPageRejected(unknown, /event_type/u);

    const payload = clone(example);
    payload.events[0].payload.crop_type = "tampered";
    expectPageRejected(payload, /payload_sha256/u);
  });

  await context.test("integrity, stable identity and final snapshot mismatch", () => {
    const integrity = clone(example);
    integrity.events[0].integrity_sha256 = "0".repeat(64);
    expectPageRejected(integrity, /integrity_sha256/u);

    const identity = clone(example);
    identity.events[0].event_id = `presentation_${"0".repeat(32)}`;
    expectPageRejected(identity, /event_id/u);

    const snapshot = clone(example);
    snapshot.snapshot_state_hash = "0".repeat(64);
    expectPageRejected(snapshot, /snapshot state hash/u);
  });
});

test("v0.5 publishes one additive read-only HTTP presentation operation", () => {
  const openapi = json(OPENAPI_PATH);
  assert.equal(openapi.openapi, "3.1.0");
  assert.equal(openapi.info.version, "0.5.0");
  assert.deepEqual(Object.keys(openapi.paths), ["/v1/worlds/{world_id}/presentation-events"]);
  const pathItem = openapi.paths["/v1/worlds/{world_id}/presentation-events"];
  assert.deepEqual(Object.keys(pathItem), ["get"]);
  const operation = pathItem.get;
  assert.equal(operation.operationId, "listWorldPresentationEvents");
  assert.equal(operation.responses["200"].content["application/json"].schema.$ref,
    "../schemas/game/world-presentation-event-page.schema.json");
  assert.deepEqual(
    operation.parameters.map((parameter) => parameter.$ref),
    [
      "#/components/parameters/RequestId",
      "#/components/parameters/TraceId",
      "#/components/parameters/CorrelationId",
      "#/components/parameters/SchemaVersion",
      "#/components/parameters/WorldId",
      "#/components/parameters/AfterSequence",
      "#/components/parameters/PageLimit",
    ],
  );
  assert.match(operation["x-invariants"].join("\n"), /GET-only/u);
  assert.match(operation["x-invariants"].join("\n"), /fail closed/u);
});
