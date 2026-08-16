import { canonicalJsonSha256V1 } from "./canonical-json.mjs";

/** Cross-field invariants that JSON Schema cannot express. */

export class SemanticInvariantError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "SemanticInvariantError";
    this.code = code;
    this.details = Object.freeze({ ...details });
  }
}

function fail(code, message, details = {}) {
  throw new SemanticInvariantError(code, message, details);
}

function integer(value, label, minimum = 0) {
  if (!Number.isInteger(value) || value < minimum) fail("INVALID_REQUEST", `${label} must be an integer >= ${minimum}`);
  return value;
}

function array(value, label, allowEmpty) {
  if (!Array.isArray(value) || (!allowEmpty && value.length === 0)) {
    fail("INVALID_REQUEST", `${label} must be ${allowEmpty ? "an array" : "a non-empty array"}`);
  }
  return value;
}

function object(value, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    fail("INVALID_REQUEST", `${label} must be an object`);
  }
  return value;
}

function text(value, label) {
  if (typeof value !== "string" || value.length === 0) {
    fail("INVALID_REQUEST", `${label} must be non-empty text`);
  }
  return value;
}

function contiguous(events, expectedFirst, label) {
  const eventIds = new Set();
  let expected = expectedFirst;
  events.forEach((rawEvent, index) => {
    const event = object(rawEvent, `${label}.events[${index}]`);
    const sequence = integer(event.sequence, `${label}.events[${index}].sequence`, 1);
    if (sequence !== expected) {
      fail("EVENT_SEQUENCE_GAP", `${label} is not gap-free`, {
        expected_sequence: expected,
        actual_sequence: sequence,
      });
    }
    if (typeof event.event_id !== "string" || event.event_id.length === 0) {
      fail("INVALID_REQUEST", `${label}.events[${index}].event_id must be text`);
    }
    if (eventIds.has(event.event_id)) {
      fail("INVALID_REQUEST", `${label} contains duplicate event_id`, { event_id: event.event_id });
    }
    eventIds.add(event.event_id);
    expected += 1;
  });
  return expected - 1;
}

export function assertClientEventBatch(batch) {
  object(batch, "batch");
  const first = integer(batch.first_sequence, "batch.first_sequence", 1);
  const last = integer(batch.last_sequence, "batch.last_sequence", 1);
  const events = array(batch.events, "batch.events", false);
  const actualLast = contiguous(events, first, "client event batch");
  if (events[0].sequence !== first || actualLast !== last) {
    fail("EVENT_SEQUENCE_GAP", "client event batch boundaries disagree with its events", {
      declared_first: first,
      declared_last: last,
      actual_first: events[0].sequence,
      actual_last: actualLast,
    });
  }
}

export function assertWorldRevisionAdvance(previousRevision, worldRevision, label = "world commit") {
  const previous = integer(previousRevision, `${label}.previous_revision`);
  const current = integer(worldRevision, `${label}.world_revision`, 1);
  if (current !== previous + 1) {
    fail("INVARIANT_VIOLATION", `${label} must advance exactly one revision`, {
      previous_revision: previous,
      world_revision: current,
    });
  }
}

export function assertEventSequenceRange(firstSequence, lastSequence, label = "event range") {
  const first = integer(firstSequence, `${label}.first_event_sequence`, 1);
  const last = integer(lastSequence, `${label}.last_event_sequence`, 1);
  if (first > last) {
    fail("INVARIANT_VIOLATION", `${label} event sequence range is reversed`, {
      first_event_sequence: first,
      last_event_sequence: last,
    });
  }
}

export function assertUniqueEvidenceRefs(rawRefs, label = "evidence_refs") {
  const refs = array(rawRefs, label, true);
  const evidenceIds = new Set();
  refs.forEach((rawRef, index) => {
    const ref = object(rawRef, `${label}[${index}]`);
    const evidenceId = text(ref.evidence_id, `${label}[${index}].evidence_id`);
    if (!/^evidence_[A-Za-z0-9_-]{8,128}$/u.test(evidenceId)) {
      fail("INVALID_REQUEST", `${label}[${index}].evidence_id is invalid`);
    }
    if (evidenceIds.has(evidenceId)) {
      fail("INVARIANT_VIOLATION", `${label} contains conflicting references for one evidence_id`, {
        evidence_id: evidenceId,
      });
    }
    evidenceIds.add(evidenceId);
  });
  return refs;
}

/** Verifies the immutable reference and canonical digest of every Evidence kind. */
export function assertEvidenceIntegrity(rawEvidence) {
  const evidence = object(rawEvidence, "Evidence");
  const payload = object(evidence.payload, "Evidence.payload");
  const reference = object(evidence.evidence_ref, "Evidence.evidence_ref");
  const integrity = object(evidence.integrity, "Evidence.integrity");
  let calculatedPayloadSha256;
  try {
    calculatedPayloadSha256 = canonicalJsonSha256V1(payload);
  } catch {
    fail("INVARIANT_VIOLATION", "Evidence.payload is outside YAYA_CANONICAL_JSON_V1");
  }
  const coherent = integrity.payload_sha256 === calculatedPayloadSha256
    && (reference.sha256 === undefined || reference.sha256 === integrity.payload_sha256)
    && Date.parse(evidence.recorded_at) >= Date.parse(evidence.occurred_at);
  if (!coherent) {
    fail("INVARIANT_VIOLATION", "Evidence immutable reference, time or payload digest is inconsistent");
  }
  return evidence;
}

/** Adds WORLD_COMMIT source and revision semantics to generic Evidence integrity. */
export function assertWorldCommitEvidence(rawEvidence) {
  const evidence = assertEvidenceIntegrity(rawEvidence);
  const payload = evidence.payload;
  if (payload.evidence_kind !== "WORLD_COMMIT") {
    fail("INVALID_REQUEST", "WORLD_COMMIT evidence payload discriminator is invalid");
  }
  assertWorldRevisionAdvance(
    payload.previous_revision,
    payload.world_revision,
    "WORLD_COMMIT evidence",
  );
  assertEventSequenceRange(
    payload.first_event_sequence,
    payload.last_event_sequence,
    "WORLD_COMMIT evidence",
  );
  const source = object(evidence.source, "WORLD_COMMIT evidence.source");
  const reference = object(evidence.evidence_ref, "WORLD_COMMIT evidence.evidence_ref");
  const coherent = source.source_type === "WORLD"
    && source.source_id === payload.world_id
    && source.world_id === payload.world_id
    && reference.evidence_type === payload.evidence_kind
    && reference.created_at === evidence.occurred_at
    && Date.parse(evidence.recorded_at) >= Date.parse(evidence.occurred_at);
  if (!coherent) {
    fail(
      "INVARIANT_VIOLATION",
      "WORLD_COMMIT evidence source, time or integrity identity is inconsistent",
    );
  }
  return evidence;
}

/**
 * Enforces the Agent feedback invariants that JSON Schema cannot express,
 * especially identity equality across the event envelope and payload.
 * Producers MUST call this after structural schema validation and before
 * persisting or publishing agent.turn.feedback_ready.
 */
export function assertAgentTurnFeedbackReadyEvent(rawEvent) {
  const event = object(rawEvent, "agent turn feedback event");
  if (event.event_type !== "agent.turn.feedback_ready") {
    fail("INVALID_REQUEST", "agent turn feedback event_type is invalid");
  }
  const envelopeCommandId = text(event.command_id, "event.command_id");
  const payload = object(event.payload, "event.payload");
  text(payload.session_id, "event.payload.session_id");
  text(payload.turn_id, "event.payload.turn_id");
  if (event.stream_id !== `agent-session:${payload.session_id}`) {
    fail(
      "INVARIANT_VIOLATION",
      "agent feedback stream_id must identify payload.session_id",
      { stream_id: event.stream_id, session_id: payload.session_id },
    );
  }
  const payloadCommandId = text(payload.command_id, "event.payload.command_id");
  if (payloadCommandId !== envelopeCommandId) {
    fail(
      "INVARIANT_VIOLATION",
      "agent feedback payload.command_id must equal envelope command_id",
      { envelope_command_id: envelopeCommandId, payload_command_id: payloadCommandId },
    );
  }
  if (payload.run_id !== null) text(payload.run_id, "event.payload.run_id");
  text(payload.message_key, "event.payload.message_key");
  text(payload.message, "event.payload.message");
  text(payload.completed_at, "event.payload.completed_at");
  assertUniqueEvidenceRefs(payload.evidence_refs, "event.payload.evidence_refs");
  if (typeof payload.degraded !== "boolean") {
    fail("INVALID_REQUEST", "event.payload.degraded must be boolean");
  }
  if (payload.degraded) {
    if (payload.source !== "provider_fallback"
      || typeof payload.fallback_reason !== "string"
      || !/^[A-Z][A-Z0-9_]{2,95}$/u.test(payload.fallback_reason)) {
      fail(
        "INVARIANT_VIOLATION",
        "degraded agent feedback must use provider_fallback with one machine-readable reason",
      );
    }
  } else if (payload.source !== "provider" || payload.fallback_reason !== null) {
    fail(
      "INVARIANT_VIOLATION",
      "non-degraded agent feedback must use provider without a fallback reason",
    );
  }
}

export function assertWorldEventPage(page, { expectedAfterSequence } = {}) {
  object(page, "page");
  if (typeof page.world_id !== "string" || page.world_id.length === 0) {
    fail("INVALID_REQUEST", "page.world_id must be text");
  }
  const from = integer(page.from_sequence, "page.from_sequence");
  const to = integer(page.to_sequence, "page.to_sequence");
  const next = integer(page.next_after_sequence, "page.next_after_sequence");
  const hasExpected = expectedAfterSequence !== undefined;
  if (hasExpected) integer(expectedAfterSequence, "expectedAfterSequence");
  const events = array(page.events, "page.events", true);
  if (events.length === 0) {
    const expected = hasExpected ? expectedAfterSequence : from;
    if (from !== expected || to !== expected || next !== expected) {
      fail("EVENT_SEQUENCE_GAP", "empty world event page advanced or changed its cursor");
    }
    return;
  }
  const actualFirst = integer(object(events[0], "page.events[0]").sequence, "page.events[0].sequence", 1);
  const expectedFirst = hasExpected ? expectedAfterSequence + 1 : actualFirst;
  const actualLast = contiguous(events, expectedFirst, "world event page");
  events.forEach((event, index) => {
    if (event.stream_id !== `world:${page.world_id}`) {
      fail("EVENT_SEQUENCE_GAP", "world event stream does not match page.world_id", {
        event_index: index,
        stream_id: event.stream_id,
      });
    }
  });
  if (from !== actualFirst || to !== actualLast || next !== actualLast) {
    fail("EVENT_SEQUENCE_GAP", "world event page cursors disagree with its events", {
      actual_first: actualFirst,
      actual_last: actualLast,
    });
  }
}

export function assertClassInsightsPrivacy(result) {
  object(result, "result");
  const privacy = object(result.privacy, "result.privacy");
  const requested = integer(privacy.minimum_cohort_size, "privacy.minimum_cohort_size", 5);
  const effective = integer(privacy.effective_minimum_cohort_size, "privacy.effective_minimum_cohort_size", 5);
  if (effective < requested) {
    fail("INVARIANT_VIOLATION", "effective privacy threshold is lower than requested", { requested, effective });
  }
  const cohortSize = integer(result.cohort_size, "result.cohort_size");
  const insights = array(result.insights, "result.insights", true);
  insights.forEach((rawInsight, index) => {
    const insight = object(rawInsight, `result.insights[${index}]`);
    if (typeof insight.suppressed !== "boolean") fail("INVALID_REQUEST", `insight ${index} suppressed must be boolean`);
    if (cohortSize < effective && !insight.suppressed) {
      fail("INVARIANT_VIOLATION", "all insights must be suppressed for a small cohort", { index });
    }
    if (insight.suppressed) {
      if (insight.learner_count !== null || insight.ratio !== null) {
        fail("INVARIANT_VIOLATION", "suppressed insight leaked a count or ratio", { index });
      }
      return;
    }
    const count = integer(insight.learner_count, `insight ${index} learner_count`);
    if (count < effective) {
      fail("INVARIANT_VIOLATION", "unsuppressed insight is below effective threshold", {
        index, learner_count: count, effective,
      });
    }
    if (typeof insight.ratio !== "number" || !Number.isFinite(insight.ratio) || insight.ratio < 0 || insight.ratio > 1) {
      fail("INVALID_REQUEST", `insight ${index} ratio must be between 0 and 1`);
    }
  });
}
