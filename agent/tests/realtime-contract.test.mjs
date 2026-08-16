import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import {
  assertSchema,
  loadDocuments,
  PROJECT_ROOT,
  resolveReference,
} from "../scripts/validate-contracts.mjs";

const ASYNCAPI_PATH = resolve(
  PROJECT_ROOT,
  "contracts/asyncapi/runtime-events.asyncapi.json",
);
const BOOTSTRAP_SCHEMA_PATH = resolve(
  PROJECT_ROOT,
  "contracts/schemas/game/bootstrap-response.schema.json",
);

function json(path) {
  return JSON.parse(readFileSync(resolve(PROJECT_ROOT, path), "utf8"));
}

function clone(value) {
  return structuredClone(value);
}

function example(name) {
  return json(`contracts/examples/${name}.json`).value;
}

function interfaceFields(source, name) {
  const match = source.match(
    new RegExp(`export interface ${name}[^\\{]*\\{([\\s\\S]*?)\\n\\}`, "u"),
  );
  assert.ok(match, `${name} is missing`);
  return [...match[1].matchAll(/^\s*readonly ([a-z][a-z0-9_]*):/gmu)]
    .map((item) => item[1])
    .sort();
}

test("bootstrap publishes a complete TLS-only realtime recovery endpoint", () => {
  const { documents } = loadDocuments();
  const schema = documents.get(BOOTSTRAP_SCHEMA_PATH);
  const value = example("game-bootstrap-response");
  assert.doesNotThrow(() => assertSchema(
    value,
    schema,
    BOOTSTRAP_SCHEMA_PATH,
    documents,
  ));

  assert.deepEqual(
    schema.properties.world.required,
    [
      "world_id",
      "revision",
      "stream_id",
      "last_event_sequence",
      "stream_protocol_version",
      "snapshot_url",
      "events_url",
      "stream_url",
    ],
  );
  assert.equal(value.world.stream_id, `world:${value.world.world_id}`);
  assert.equal(value.world.stream_protocol_version, "1.0.0");
  assert.equal(new URL(value.world.stream_url).protocol, "wss:");

  const invalid = [];
  for (const field of [
    "stream_id",
    "last_event_sequence",
    "stream_protocol_version",
    "stream_url",
  ]) {
    const mutation = clone(value);
    delete mutation.world[field];
    invalid.push(mutation);
  }
  for (const streamUrl of [
    "ws://api.yaya.example/v1/realtime",
    "https://api.yaya.example/v1/realtime",
    "wss://token:secret@api.yaya.example/v1/realtime",
    "wss://api.yaya.example/v1/realtime?token=secret",
    "wss://api.yaya.example/v1/realtime#fragment",
  ]) {
    const mutation = clone(value);
    mutation.world.stream_url = streamUrl;
    invalid.push(mutation);
  }
  for (const [field, invalidValue] of [
    ["stream_protocol_version", "2.0.0"],
    ["last_event_sequence", -1],
    ["last_event_sequence", true],
  ]) {
    const mutation = clone(value);
    mutation.world[field] = invalidValue;
    invalid.push(mutation);
  }
  const extra = clone(value);
  extra.world.silent_future_field = true;
  invalid.push(extra);

  for (const mutation of invalid) {
    assert.throws(() => assertSchema(
      mutation,
      schema,
      BOOTSTRAP_SCHEMA_PATH,
      documents,
    ));
  }
});

test("AsyncAPI binds bootstrap to authenticated WSS and a pinned subprotocol", () => {
  const contract = json("contracts/asyncapi/runtime-events.asyncapi.json");
  const bootstrap = example("game-bootstrap-response").world;
  const server = contract.servers.runtimeWss;
  const channel = contract.channels.worldRealtime;
  const binding = channel.bindings.ws;
  const headers = contract.components.schemas.WebSocketUpgradeHeaders;

  assert.equal(server.protocol, "wss");
  assert.equal(server.protocolVersion, "13");
  assert.deepEqual(server.security, [
    { $ref: "#/components/securitySchemes/bearerAuth" },
  ]);
  assert.deepEqual(channel.servers, [{ $ref: "#/servers/runtimeWss" }]);
  assert.equal(binding.method, "GET");
  assert.equal(binding.bindingVersion, "0.1.0");
  assert.equal(
    binding.headers.$ref,
    "#/components/schemas/WebSocketUpgradeHeaders",
  );
  assert.deepEqual(headers.required, [
    "Authorization",
    "X-Request-Id",
    "X-Trace-Id",
    "X-Correlation-Id",
    "X-Schema-Version",
    "X-Stream-Protocol-Version",
    "Sec-WebSocket-Protocol",
  ]);
  assert.deepEqual(
    headers.properties["Sec-WebSocket-Protocol"].enum,
    ["yaya.runtime.v1"],
  );
  assert.equal(contract.components.securitySchemes.bearerAuth.bearerFormat, "JWT");
  assert.match(
    contract.components.securitySchemes.bearerAuth.description,
    /Production profile.*local Mock.*test-only/u,
  );

  const advertised = new URL(bootstrap.stream_url);
  assert.equal(advertised.host, server.host);
  assert.equal(advertised.pathname, channel.address);
  assert.equal(
    bootstrap.stream_protocol_version,
    contract.components.schemas.RealtimeProtocolVersion.const,
  );
});

test("control-frame schemas are closed unions with positive and negative cases", () => {
  const { documents } = loadDocuments();
  const contract = documents.get(ASYNCAPI_PATH);
  const frames = new Map([
    ["SubscribeFrame", example("realtime-subscribe-frame")],
    ["ResumeFrame", example("realtime-resume-frame")],
    ["AckFrame", example("realtime-ack-frame")],
    ["HeartbeatAckFrame", example("realtime-heartbeat-ack-frame")],
    ["SubscribedFrame", example("realtime-subscribed-frame")],
    ["HeartbeatFrame", example("realtime-heartbeat-frame")],
    ["RealtimeErrorFrame", example("realtime-error-frame")],
  ]);

  for (const [name, value] of frames) {
    const schema = contract.components.schemas[name];
    assert.equal(schema.additionalProperties, false, `${name} must be closed`);
    assert.doesNotThrow(
      () => assertSchema(value, schema, ASYNCAPI_PATH, documents),
      name,
    );

    const missingDiscriminator = clone(value);
    delete missingDiscriminator.frame_type;
    assert.throws(() => assertSchema(
      missingDiscriminator,
      schema,
      ASYNCAPI_PATH,
      documents,
    ));

    const unknownField = { ...value, ignored_typo: true };
    assert.throws(() => assertSchema(
      unknownField,
      schema,
      ASYNCAPI_PATH,
      documents,
    ));

    const wrongVersion = { ...value, protocol_version: "2.0.0" };
    assert.throws(() => assertSchema(
      wrongVersion,
      schema,
      ASYNCAPI_PATH,
      documents,
    ));
  }

  const invalidAck = { ...frames.get("AckFrame"), sequence: 0 };
  assert.throws(() => assertSchema(
    invalidAck,
    contract.components.schemas.AckFrame,
    ASYNCAPI_PATH,
    documents,
  ));
  const fatalWithoutClose = {
    ...frames.get("RealtimeErrorFrame"),
    close_code: null,
  };
  assert.throws(() => assertSchema(
    fatalWithoutClose,
    contract.components.schemas.RealtimeErrorFrame,
    ASYNCAPI_PATH,
    documents,
  ));
  const nonFatalWithClose = {
    ...frames.get("RealtimeErrorFrame"),
    fatal: false,
  };
  assert.throws(() => assertSchema(
    nonFatalWithClose,
    contract.components.schemas.RealtimeErrorFrame,
    ASYNCAPI_PATH,
    documents,
  ));
  const mismatchedClose = {
    ...frames.get("RealtimeErrorFrame"),
    close_code: 4401,
  };
  assert.throws(() => assertSchema(
    mismatchedClose,
    contract.components.schemas.RealtimeErrorFrame,
    ASYNCAPI_PATH,
    documents,
  ));
  const nonRetryableWithDelay = {
    ...frames.get("RealtimeErrorFrame"),
    close_code: 4401,
    retry_after_ms: 1000,
    error: {
      code: "AUTHENTICATION_REQUIRED",
      category: "AUTHENTICATION",
      retryable: false,
      user_message_key: "auth.login_required",
      stage: "REALTIME_HANDSHAKE",
    },
  };
  assert.throws(() => assertSchema(
    nonRetryableWithDelay,
    contract.components.schemas.RealtimeErrorFrame,
    ASYNCAPI_PATH,
    documents,
  ));

  assert.equal(
    contract.components.schemas.RealtimeClientFrame.oneOf.length,
    4,
  );
  assert.equal(
    contract.components.schemas.RealtimeServerControlFrame.oneOf.length,
    3,
  );
});

test("WSS world events and HTTP backfill share one event item schema", () => {
  const { documents } = loadDocuments();
  const contract = documents.get(ASYNCAPI_PATH);
  const page = json("contracts/schemas/game/world-event-page.schema.json");
  const liveSchema = contract.components.schemas.RealtimeWorldEvent;

  assert.equal(
    liveSchema.$ref,
    "../schemas/game/world-event-page.schema.json#/properties/events/items",
  );
  assert.deepEqual(
    resolveReference(ASYNCAPI_PATH, liveSchema.$ref, documents).value,
    page.properties.events.items,
  );
  assert.equal(
    contract.channels.worldRealtime.messages.WorldEvent.payload.$ref,
    "#/components/schemas/RealtimeWorldEvent",
  );
  for (const event of example("game-world-event-page").events) {
    assert.doesNotThrow(() => assertSchema(
      event,
      liveSchema,
      ASYNCAPI_PATH,
      documents,
    ));
  }

  const semantics = contract.channels.worldRealtime["x-delivery-semantics"];
  assert.equal(semantics.guarantee, "at-least-once");
  for (const field of [
    "checkpoint",
    "deduplication",
    "gap_recovery",
    "reconnect",
    "ordering",
  ]) {
    assert.equal(typeof semantics[field], "string");
    assert.ok(semantics[field].length > 40, `${field} semantics are underspecified`);
  }
});

test("realtime close codes are complete and only use catalog errors", () => {
  const contract = json("contracts/asyncapi/runtime-events.asyncapi.json");
  const catalog = new Set(
    json("contracts/error-catalog.json").errors.map((entry) => entry.code),
  );
  const schema = contract.components.schemas.RealtimeErrorFrame;
  const closeCodes = contract.components.schemas.RealtimeCloseCode.enum;
  assert.deepEqual(
    Object.keys(schema["x-close-codes"]).map(Number).sort((a, b) => a - b),
    [...closeCodes].sort((a, b) => a - b),
  );
  for (const codes of Object.values(schema["x-close-codes"])) {
    for (const code of codes) assert.ok(catalog.has(code), code);
  }
  const error = example("realtime-error-frame");
  assert.ok(
    schema["x-close-codes"][String(error.close_code)].includes(error.error.code),
  );
});

test("Godot bootstrap validation cannot drift from the realtime wire fields", () => {
  const schema = json("contracts/schemas/game/bootstrap-response.schema.json");
  const source = readFileSync(
    resolve(PROJECT_ROOT, "clients/godot/contract_validator.gd"),
    "utf8",
  );
  const start = source.indexOf("static func validate_bootstrap_response");
  const end = source.indexOf("\nstatic func ", start + 1);
  assert.ok(start >= 0 && end > start, "Godot bootstrap validator is missing");
  const body = source.slice(start, end);
  for (const field of schema.properties.world.required) {
    assert.match(body, new RegExp(`"${field}"`, "u"), field);
  }
  assert.match(body, /stream_id != "world:%s" % value\.world\.world_id/u);
  assert.match(body, /stream_protocol_version != REALTIME_PROTOCOL_VERSION/u);
  assert.match(body, /\^wss:\/\//u);
  assert.match(body, /without query or fragment/u);
});

test("TypeScript realtime interfaces exactly mirror every control-frame schema", () => {
  const contract = json("contracts/asyncapi/runtime-events.asyncapi.json");
  const source = readFileSync(
    resolve(PROJECT_ROOT, "src/domain/realtime.d.ts"),
    "utf8",
  );
  const pairs = new Map([
    ["SubscribeFrame", "RealtimeSubscribeFrame"],
    ["ResumeFrame", "RealtimeResumeFrame"],
    ["AckFrame", "RealtimeAckFrame"],
    ["HeartbeatAckFrame", "RealtimeHeartbeatAckFrame"],
    ["SubscribedFrame", "RealtimeSubscribedFrame"],
    ["HeartbeatFrame", "RealtimeHeartbeatFrame"],
    ["RealtimeErrorFrame", "RealtimeErrorFrame"],
  ]);
  for (const [schemaName, interfaceName] of pairs) {
    assert.deepEqual(
      interfaceFields(source, interfaceName),
      [...contract.components.schemas[schemaName].required].sort(),
      `${interfaceName} drifted from ${schemaName}`,
    );
  }
  assert.match(source, /export type RealtimeProtocolVersion = "1\.0\.0";/u);
  assert.match(source, /export type RealtimeWorldEvent = DomainEvent;/u);
});
