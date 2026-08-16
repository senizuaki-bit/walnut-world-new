import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { PROJECT_ROOT } from "../scripts/validate-contracts.mjs";
import {
  assertClassInsightsPrivacy,
  assertClientEventBatch,
  assertEventSequenceRange,
  assertWorldRevisionAdvance,
  assertWorldEventPage,
  SemanticInvariantError,
} from "../src/semantic-invariants.mjs";

function example(name) {
  return JSON.parse(readFileSync(resolve(PROJECT_ROOT, "contracts/examples", name), "utf8")).value;
}

function rejects(action, code) {
  assert.throws(action, (error) => error instanceof SemanticInvariantError && error.code === code);
}

test("world commits advance exactly one revision", () => {
  assert.doesNotThrow(() => assertWorldRevisionAdvance(184, 185));
  rejects(() => assertWorldRevisionAdvance(184, 186), "INVARIANT_VIOLATION");
  rejects(() => assertWorldRevisionAdvance(184, 184), "INVARIANT_VIOLATION");
});

test("world commit event ranges cannot be reversed", () => {
  assert.doesNotThrow(() => assertEventSequenceRange(732, 733));
  assert.doesNotThrow(() => assertEventSequenceRange(732, 732));
  rejects(() => assertEventSequenceRange(733, 732), "INVARIANT_VIOLATION");
  rejects(() => assertEventSequenceRange(0, 1), "INVALID_REQUEST");
});

test("world event page rejects gaps, duplicates, wrong streams and cursor drift", () => {
  const valid = example("game-world-event-page.json");
  assert.doesNotThrow(() => assertWorldEventPage(valid, { expectedAfterSequence: 731 }));

  const gap = structuredClone(valid);
  gap.events[1].sequence = 734;
  gap.to_sequence = 734;
  gap.next_after_sequence = 734;
  rejects(() => assertWorldEventPage(gap, { expectedAfterSequence: 731 }), "EVENT_SEQUENCE_GAP");

  const duplicate = structuredClone(valid);
  duplicate.events[1].event_id = duplicate.events[0].event_id;
  rejects(() => assertWorldEventPage(duplicate, { expectedAfterSequence: 731 }), "INVALID_REQUEST");

  const wrongStream = structuredClone(valid);
  wrongStream.events[1].stream_id = "world:another_world";
  rejects(() => assertWorldEventPage(wrongStream, { expectedAfterSequence: 731 }), "EVENT_SEQUENCE_GAP");

  const cursorDrift = structuredClone(valid);
  cursorDrift.next_after_sequence += 1;
  rejects(() => assertWorldEventPage(cursorDrift, { expectedAfterSequence: 731 }), "EVENT_SEQUENCE_GAP");
});

test("empty page cannot silently advance its consumer cursor", () => {
  const page = {
    world_id: "world_demo_001",
    from_sequence: 733,
    to_sequence: 733,
    next_after_sequence: 733,
    events: [],
  };
  assert.doesNotThrow(() => assertWorldEventPage(page, { expectedAfterSequence: 733 }));
  page.next_after_sequence = 734;
  rejects(() => assertWorldEventPage(page, { expectedAfterSequence: 733 }), "EVENT_SEQUENCE_GAP");
});

test("client event batch rejects boundary, gap and identity contradictions", () => {
  const valid = example("game-client-event-batch-request.json");
  assert.doesNotThrow(() => assertClientEventBatch(valid));

  const wrongLast = structuredClone(valid);
  wrongLast.last_sequence += 1;
  rejects(() => assertClientEventBatch(wrongLast), "EVENT_SEQUENCE_GAP");

  const duplicate = structuredClone(valid);
  if (duplicate.events.length === 1) {
    duplicate.events.push(structuredClone(duplicate.events[0]));
    duplicate.events[1].sequence += 1;
    duplicate.last_sequence += 1;
  } else {
    duplicate.events[1].event_id = duplicate.events[0].event_id;
  }
  rejects(() => assertClientEventBatch(duplicate), "INVALID_REQUEST");
});

test("dynamic class privacy threshold cannot be bypassed", () => {
  const valid = example("feishu-class-insights-response.json");
  assert.doesNotThrow(() => assertClassInsightsPrivacy(valid));

  const downgraded = structuredClone(valid);
  downgraded.privacy.minimum_cohort_size = 10;
  downgraded.privacy.effective_minimum_cohort_size = 5;
  rejects(() => assertClassInsightsPrivacy(downgraded), "INVARIANT_VIOLATION");

  const smallCell = structuredClone(valid);
  smallCell.privacy.effective_minimum_cohort_size = 10;
  rejects(() => assertClassInsightsPrivacy(smallCell), "INVARIANT_VIOLATION");

  const smallCohort = structuredClone(valid);
  smallCohort.cohort_size = 4;
  rejects(() => assertClassInsightsPrivacy(smallCohort), "INVARIANT_VIOLATION");
});
