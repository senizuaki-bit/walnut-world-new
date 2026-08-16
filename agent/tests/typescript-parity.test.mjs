import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { verifyPortSurface } from "../scripts/port-surface.mjs";
import { PROJECT_ROOT } from "../scripts/validate-contracts.mjs";

function source(path) {
  return readFileSync(resolve(PROJECT_ROOT, path), "utf8");
}

function json(path) {
  return JSON.parse(source(path));
}

function sourceOverride(targetPath, replacement) {
  const absoluteTarget = resolve(targetPath);
  return (path) => (
    resolve(path) === absoluteTarget ? replacement : readFileSync(path, "utf8")
  );
}

function mutateSource(text, pattern, replacement, description) {
  const mutated = text.replace(pattern, replacement);
  assert.notEqual(mutated, text, `${description} fixture mutation was not applied`);
  return mutated;
}

function appendSource(text, addition, description) {
  const newline = text.includes("\r\n") ? "\r\n" : "\n";
  return mutateSource(
    text,
    /$/u,
    `${newline}${addition.replaceAll("\n", newline)}${newline}`,
    description,
  );
}

function unionMembers(text, typeName) {
  const match = text.match(new RegExp(`export type ${typeName} =([\\s\\S]*?);`, "u"));
  assert.ok(match, `${typeName} union is missing`);
  return [...match[1].matchAll(/"([A-Z][A-Z0-9_]+)"/gu)].map((item) => item[1]).sort();
}

function collectEventTypes(value, output = new Set()) {
  if (value === null || typeof value !== "object") return output;
  if (typeof value.event_type?.const === "string") output.add(value.event_type.const);
  for (const child of Object.values(value)) collectEventTypes(child, output);
  return output;
}

function interfaceBody(text, interfaceName) {
  const marker = `export interface ${interfaceName}`;
  const start = text.indexOf(marker);
  assert.notEqual(start, -1, `${interfaceName} is missing`);
  const open = text.indexOf("{", start);
  let depth = 0;
  for (let index = open; index < text.length; index += 1) {
    if (text[index] === "{") depth += 1;
    if (text[index] === "}") depth -= 1;
    if (depth === 0) return text.slice(open + 1, index);
  }
  assert.fail(`${interfaceName} has an unclosed body`);
}

function methodParameterNames(body, methodName) {
  const method = new RegExp(`\\b${methodName}(?:<[^>]+>)?\\s*\\(`, "u").exec(body);
  assert.ok(method, `${methodName} is missing`);
  const open = body.indexOf("(", method.index);
  let depth = 0;
  for (let index = open; index < body.length; index += 1) {
    if (body[index] === "(") depth += 1;
    if (body[index] === ")") depth -= 1;
    if (depth === 0) {
      return body.slice(open + 1, index).split(",")
        .map((parameter) => parameter.trim())
        .filter(Boolean)
        .map((parameter) => parameter.match(/^([A-Za-z][A-Za-z0-9]*)\??\s*:/u)?.[1])
        .map((name) => {
          assert.ok(name, `${methodName} contains an unparsable parameter`);
          return name;
        });
    }
  }
  assert.fail(`${methodName} has an unclosed parameter list`);
}

test("TypeScript CommandType and error-code unions exactly mirror contracts", () => {
  const commandSchema = json("contracts/schemas/common/command-type.schema.json");
  const catalog = json("contracts/error-catalog.json");
  assert.deepEqual(
    unionMembers(source("src/domain/commands.d.ts"), "CommandType"),
    [...commandSchema.enum].sort(),
  );
  assert.deepEqual(
    unionMembers(source("src/domain/result.d.ts"), "ContractErrorCode"),
    catalog.errors.map((entry) => entry.code).sort(),
  );
});

test("TypeScript runtime event map exactly mirrors AsyncAPI event types", () => {
  const asyncApi = json("contracts/asyncapi/runtime-events.asyncapi.json");
  const expected = [...collectEventTypes(asyncApi.components.schemas)].sort();
  const eventSource = source("src/domain/events.d.ts");
  const map = eventSource.match(/export interface RuntimeEventPayloadMap \{([\s\S]*?)\n\}/u);
  assert.ok(map, "RuntimeEventPayloadMap is missing");
  const actual = [...map[1].matchAll(/readonly "([a-z][a-z0-9_.-]+)":/gu)]
    .map((item) => item[1]).sort();
  assert.deepEqual(actual, expected);
});

test("TypeScript EventStore keeps domain streams open and stream identity singular", () => {
  const eventSource = source("src/domain/events.d.ts");
  const portSource = source("src/ports/event-store.port.d.ts");
  assert.match(eventSource, /export interface DomainEvent<[\s\S]*?Type extends string = string/u);
  assert.match(
    eventSource,
    /export type EventEnvelope<[\s\S]*?= DomainEvent<Type, Payload> & \{ readonly schema_version: "1\.0\.0" \};/u,
  );
  assert.match(
    eventSource,
    /export type EventEnvelopeV2<[\s\S]*?Type extends "learner\.inference\.recorded"[\s\S]*?= DomainEventV2<Type, Payload>;/u,
  );
  assert.match(
    eventSource,
    /export type UncommittedEvent<[\s\S]*?"event_id" \| "stream_id" \| "sequence" \| "occurred_at"/u,
  );
  assert.match(portSource, /events: readonly UncommittedEvent\[\]/u);
  assert.match(portSource, /AsyncResult<CursorPage<DomainEvent>, ContractError>/u);
  assert.doesNotMatch(portSource, /RuntimeEvent/u);
});

test("TypeScript WorldSnapshot uses transport field names", () => {
  const worldSource = source("src/domain/world.d.ts");
  const snapshot = worldSource.match(/export interface WorldSnapshot[^\{]*\{([\s\S]*?)\n\}/u);
  assert.ok(snapshot, "WorldSnapshot is missing");
  for (const field of [
    "request_context", "world_id", "revision", "last_event_sequence",
    "state_schema_version", "state_hash", "generated_at", "world_rules_version", "state",
  ]) {
    assert.match(snapshot[1], new RegExp(`readonly ${field}:`, "u"));
  }
  assert.doesNotMatch(snapshot[1], /world_revision|captured_at/u);
});

test("TypeScript LLM responses expose an explicit fallback discriminant", () => {
  const llmSource = source("src/domain/llm.d.ts");
  assert.match(llmSource, /export type LLMResponseSource = "provider" \| "provider_fallback";/u);
  assert.match(llmSource, /readonly source: "provider";[\s\S]*readonly degraded: false;[\s\S]*readonly fallback_reason: null;/u);
  assert.match(llmSource, /readonly source: "provider_fallback";[\s\S]*readonly degraded: true;[\s\S]*readonly fallback_reason: string;/u);
});

test("Python and TypeScript port signatures exactly match the frozen cross-language surface", () => {
  const manifest = json("contracts/port-surface.json");
  assert.doesNotThrow(() => verifyPortSurface(manifest));
  const index = source("src/ports/index.d.ts");
  for (const port of manifest.ports) {
    const text = source(port.typescript_file);
    const body = interfaceBody(text, port.typescript);
    const expectedMethods = port.methods.map((method) => method.typescript).sort();
    const actualMethods = [...body.matchAll(/^\s{2}([A-Za-z][A-Za-z0-9]*)\s*(?:<[^>]+>)?\(/gmu)]
      .map((match) => match[1]).sort();
    assert.deepEqual(actualMethods, expectedMethods, `${port.typescript} method set drifted`);
    for (const method of port.methods) {
      assert.deepEqual(
        methodParameterNames(body, method.typescript),
        method.typescript_contract.parameters.map((parameter) => parameter.name),
        `${port.typescript}.${method.typescript} parameters drifted`,
      );
    }
    const exportName = port.typescript_file.split("/").at(-1).replace(/\.d\.ts$/u, ".js");
    assert.match(index, new RegExp(`export \\* from "\\./${exportName}";`, "u"));
  }
});

test("port surface freezes command actor isolation and the explicit Outbox exception", () => {
  const manifest = json("contracts/port-surface.json");
  const commandStore = manifest.ports.find((port) => port.python === "CommandStorePort");
  const outbox = manifest.ports.find((port) => port.python === "OutboxPort");

  assert.deepEqual(commandStore.idempotency_scope, {
    components: [
      "context.actor.tenant_id",
      "context.actor.actor_id",
      "operation",
      "idempotency_key",
    ],
    actor_boundary: "required",
    hash_field: "command.request_sha256",
  });
  assert.deepEqual(outbox.idempotency_scope, {
    components: [
      "message.operation_context.actor.tenant_id",
      "message.destination",
      "message.idempotency_key",
    ],
    actor_boundary: "service_delivery_exception",
    hash_field: "message.payload_sha256",
  });

  const actorlessCommand = structuredClone(manifest);
  actorlessCommand.ports.find((port) => port.python === "CommandStorePort")
    .idempotency_scope.components.splice(1, 1);
  assert.throws(
    () => verifyPortSurface(actorlessCommand),
    /CommandStorePort idempotency_scope drifted/u,
  );

  const actorScopedOutbox = structuredClone(manifest);
  actorScopedOutbox.ports.find((port) => port.python === "OutboxPort")
    .idempotency_scope.components.splice(
      1,
      0,
      "message.operation_context.actor.actor_id",
    );
  assert.throws(
    () => verifyPortSurface(actorScopedOutbox),
    /OutboxPort idempotency_scope drifted/u,
  );
});

test("port inventory lock rejects omitted, added, and removed Port declarations", () => {
  const manifest = json("contracts/port-surface.json");
  const omitted = structuredClone(manifest);
  omitted.ports = omitted.ports.filter((port) => port.python !== "DeliveryPort");
  assert.throws(
    () => verifyPortSurface(omitted),
    /Python Protocol Port set differs.*missing from manifest: DeliveryPort/u,
  );

  const omittedAlias = structuredClone(manifest);
  omittedAlias.typescript_port_aliases = omittedAlias.typescript_port_aliases.filter(
    (alias) => alias.name !== "FeishuPort",
  );
  assert.throws(
    () => verifyPortSurface(omittedAlias),
    /TypeScript exported Port alias set differs.*missing from manifest: FeishuPort/u,
  );

  const pythonPath = resolve(PROJECT_ROOT, manifest.python_file);
  const pythonSource = source(manifest.python_file);
  const addedPythonPort = appendSource(
    pythonSource,
    "class SurprisePort(Protocol):\n    async def ping(self) -> Result[None]: ...",
    "added Python Port",
  );
  assert.throws(
    () => verifyPortSurface(manifest, sourceOverride(pythonPath, addedPythonPort)),
    /Python Protocol Port set differs.*missing from manifest: SurprisePort/u,
  );

  const removedPythonPort = mutateSource(
    pythonSource,
    "class DeliveryPort(Protocol):",
    "class DeliveryBoundary(Protocol):",
    "removed Python Port",
  );
  assert.throws(
    () => verifyPortSurface(manifest, sourceOverride(pythonPath, removedPythonPort)),
    /Python Protocol Port set differs.*missing from source: DeliveryPort/u,
  );

  const typescriptPath = resolve(PROJECT_ROOT, "src/ports/audit.port.d.ts");
  const auditSource = source("src/ports/audit.port.d.ts");
  const addedTypeScriptPort = appendSource(
    auditSource,
    "export interface SurprisePort {}",
    "added TypeScript Port",
  );
  assert.throws(
    () => verifyPortSurface(
      manifest,
      sourceOverride(typescriptPath, addedTypeScriptPort),
    ),
    /TypeScript exported Port interface set differs.*missing from manifest: SurprisePort/u,
  );

  const addedTypeScriptAlias = appendSource(
    auditSource,
    "export type SurpriseAliasPort = AuditPort;",
    "added TypeScript Port alias",
  );
  assert.throws(
    () => verifyPortSurface(
      manifest,
      sourceOverride(typescriptPath, addedTypeScriptAlias),
    ),
    /TypeScript exported Port alias set differs.*missing from manifest: SurpriseAliasPort/u,
  );

  const deliverySource = source("src/ports/feishu.port.d.ts");
  const removedTypeScriptPort = mutateSource(
    deliverySource,
    "export interface DeliveryPort {",
    "export interface DeliveryBoundary {",
    "removed TypeScript Port",
  );
  const deliveryPath = resolve(PROJECT_ROOT, "src/ports/feishu.port.d.ts");
  assert.throws(
    () => verifyPortSurface(
      manifest,
      sourceOverride(deliveryPath, removedTypeScriptPort),
    ),
    /TypeScript exported Port interface set differs.*missing from source: DeliveryPort/u,
  );
});

test("port method inventory lock rejects omitted and added methods", () => {
  const manifest = json("contracts/port-surface.json");
  const omitted = structuredClone(manifest);
  omitted.ports.find((port) => port.python === "AuditPort").methods.shift();
  assert.throws(
    () => verifyPortSurface(omitted),
    /AuditPort method set differs from the manifest mapping/u,
  );

  const pythonPath = resolve(PROJECT_ROOT, manifest.python_file);
  const pythonSource = source(manifest.python_file);
  const pythonNewline = pythonSource.includes("\r\n") ? "\r\n" : "\n";
  const addedMethod = mutateSource(
    pythonSource,
    /^class PolicyPort\(Protocol\):/mu,
    `    async def probe(self) -> Result[None]: ...${pythonNewline}${pythonNewline}class PolicyPort(Protocol):`,
    "added Python method",
  );
  assert.throws(
    () => verifyPortSurface(manifest, sourceOverride(pythonPath, addedMethod)),
    /AuditPort method set differs from the manifest mapping/u,
  );

  const typescriptPath = resolve(PROJECT_ROOT, "src/ports/audit.port.d.ts");
  const auditSource = source("src/ports/audit.port.d.ts");
  const typescriptNewline = auditSource.includes("\r\n") ? "\r\n" : "\n";
  const addedTypeScriptMethod = mutateSource(
    auditSource,
    /^\}$/mu,
    `  probe(): AsyncResult<void, ContractError>;${typescriptNewline}}`,
    "added TypeScript method",
  );
  assert.throws(
    () => verifyPortSurface(
      manifest,
      sourceOverride(typescriptPath, addedTypeScriptMethod),
    ),
    /AuditPort method set differs from the manifest mapping/u,
  );
});

test("port signature lock rejects type, return, async, and error-only source drift", () => {
  const manifest = json("contracts/port-surface.json");
  const pythonPath = resolve(PROJECT_ROOT, manifest.python_file);
  const typescriptPath = resolve(PROJECT_ROOT, "src/ports/audit.port.d.ts");
  const pythonSource = source(manifest.python_file);
  const auditSource = source("src/ports/audit.port.d.ts");

  const pythonMutation = mutateSource(
    pythonSource,
    ") -> Result[AuditRecord]: ...",
    ") -> Result[None]: ...",
    "Python return type",
  );
  assert.throws(
    () => verifyPortSurface(manifest, sourceOverride(pythonPath, pythonMutation)),
    /AuditPort\.append python_contract drifted/u,
  );

  const pythonAsyncMutation = mutateSource(
    pythonSource,
    "    async def append(",
    "    def append(",
    "Python async",
  );
  assert.throws(
    () => verifyPortSurface(manifest, sourceOverride(pythonPath, pythonAsyncMutation)),
    /AuditPort\.append python_contract drifted/u,
  );

  const typescriptMutation = mutateSource(
    auditSource,
    "record: AuditRecord,",
    "record: AuditQuery,",
    "TypeScript parameter type",
  );
  assert.throws(
    () => verifyPortSurface(manifest, sourceOverride(typescriptPath, typescriptMutation)),
    /AuditPort\.append typescript_contract drifted/u,
  );

  const typescriptParameterRemoval = mutateSource(
    auditSource,
    /^[ \t]+context: OperationContext,\r?\n(?=[ \t]+\): AsyncResult<AuditRecord, ContractError>;)/mu,
    "",
    "removed TypeScript parameter",
  );
  assert.throws(
    () => verifyPortSurface(
      manifest,
      sourceOverride(typescriptPath, typescriptParameterRemoval),
    ),
    /AuditPort\.append typescript_contract drifted/u,
  );

  const typescriptErrorMutation = mutateSource(
    auditSource,
    "AsyncResult<AuditRecord, ContractError>",
    "AsyncResult<AuditRecord, never>",
    "TypeScript error type",
  );
  assert.throws(
    () => verifyPortSurface(
      manifest,
      sourceOverride(typescriptPath, typescriptErrorMutation),
    ),
    /AuditPort\.append typescript_contract drifted/u,
  );
});
