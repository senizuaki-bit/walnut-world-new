import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const TEST_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = dirname(TEST_DIRECTORY);
const GUIDE_PATH = resolve(PROJECT_ROOT, "docs/INTERFACE_INTEGRATION_GUIDE.md");

function text(relativePath) {
  return readFileSync(resolve(PROJECT_ROOT, relativePath), "utf8");
}

function json(relativePath) {
  return JSON.parse(text(relativePath));
}

function unquoteMarkdown(value) {
  const trimmed = value.trim();
  return trimmed.startsWith("`") && trimmed.endsWith("`")
    ? trimmed.slice(1, -1)
    : trimmed;
}

function collectOpenApiOperations() {
  const operations = [];
  for (const relativePath of [
    "contracts/openapi/game-api.openapi.json",
    "contracts/openapi/product-experience.openapi.json",
    "contracts/openapi/feishu-integration.openapi.json",
  ]) {
    const document = json(relativePath);
    for (const [path, pathItem] of Object.entries(document.paths)) {
      for (const method of ["get", "post", "put", "patch", "delete"]) {
        const operation = pathItem[method];
        if (operation) {
          operations.push({
            method: method.toUpperCase(),
            operationId: operation.operationId,
            path,
            successResponses: Object.keys(operation.responses)
              .filter((status) => /^2\d\d$/u.test(status))
              .sort(),
          });
        }
      }
    }
  }
  return operations.sort((left, right) => left.operationId.localeCompare(right.operationId, "en"));
}

function parseHttpOperationRows(guide) {
  const rows = new Map();
  for (const line of guide.split(/\r?\n/u)) {
    if (!line.startsWith("|")) continue;
    const cells = line.split("|").slice(1, -1).map((cell) => unquoteMarkdown(cell));
    if (!/^(GET|POST|PUT|PATCH|DELETE)$/u.test(cells[0] ?? "")) continue;
    const [method, documentedPath, operationId, documentedSuccessResponses] = cells;
    assert.ok(operationId, `HTTP guide row is missing operationId: ${line}`);
    assert.ok(documentedSuccessResponses, `HTTP guide row is missing success responses: ${line}`);
    assert.ok(!rows.has(operationId), `HTTP guide duplicates operationId ${operationId}`);
    rows.set(operationId, {
      method,
      path: documentedPath.split("?", 1)[0],
      successResponses: documentedSuccessResponses.split("/").map((status) => status.trim()).sort(),
    });
  }
  return rows;
}

function collectEventTypes(value, output = new Set()) {
  if (value === null || typeof value !== "object") return output;
  if (typeof value.event_type?.const === "string") output.add(value.event_type.const);
  for (const child of Object.values(value)) collectEventTypes(child, output);
  return output;
}

function assertInterfaceGuideAligned(guide) {
  const operations = collectOpenApiOperations();
  const documentedOperations = parseHttpOperationRows(guide);
  assert.deepEqual(
    [...documentedOperations.keys()].sort((left, right) => left.localeCompare(right, "en")),
    operations.map((operation) => operation.operationId),
    "interface guide HTTP operationId set drifted from the three OpenAPI documents",
  );
  for (const operation of operations) {
    assert.deepEqual(
      documentedOperations.get(operation.operationId),
      {
        method: operation.method,
        path: operation.path,
        successResponses: operation.successResponses,
      },
      `interface guide method/path/success status drifted for ${operation.operationId}`,
    );
  }

  const portSurface = json("contracts/port-surface.json");
  for (const port of portSurface.ports) {
    assert.ok(guide.includes("`" + port.python + "`"), `guide omits ${port.python}`);
    for (const method of port.methods) {
      assert.ok(
        guide.includes("`" + method.python + "`"),
        `guide omits ${port.python}.${method.python}`,
      );
    }
  }

  const asyncApi = json("contracts/asyncapi/runtime-events.asyncapi.json");
  const frameTypes = new Set();
  for (const schema of Object.values(asyncApi.components.schemas)) {
    const frameType = schema?.properties?.frame_type?.const;
    if (typeof frameType === "string") frameTypes.add(frameType);
  }
  assert.deepEqual(
    [...frameTypes].sort(),
    ["ack", "error", "heartbeat", "heartbeat_ack", "resume", "subscribe", "subscribed"],
    "unexpected realtime frame type set; update the guide and this intentional protocol lock",
  );
  for (const frameType of frameTypes) {
    assert.ok(guide.includes("`" + frameType + "`"), `guide omits ${frameType} frame`);
  }
  const eventTypes = collectEventTypes(asyncApi);
  assert.equal(eventTypes.size, 25, "unexpected runtime event type count");
  for (const eventType of eventTypes) {
    assert.ok(guide.includes("`" + eventType + "`"), `guide omits ${eventType} event`);
  }
  const closeCodes = asyncApi.components.schemas.RealtimeErrorFrame["x-close-codes"];
  for (const [closeCode, errorCodes] of Object.entries(closeCodes)) {
    assert.ok(guide.includes(`| ${closeCode} |`), `guide omits realtime close code ${closeCode}`);
    for (const errorCode of errorCodes) {
      assert.ok(
        guide.includes("`" + errorCode + "`"),
        `guide omits ${errorCode} for realtime close code ${closeCode}`,
      );
    }
  }
  for (const requiredText of [
    "`runtime.events.{stream_id}`",
    "`/v1/realtime`",
    "`WorldEvent`",
    "yaya.runtime.v1",
    "`agent.turn.feedback_ready`",
    "`contracts/openapi/game-api.openapi.json`",
    "`contracts/openapi/product-experience.openapi.json`",
    "`contracts/openapi/feishu-integration.openapi.json`",
    "`contracts/asyncapi/runtime-events.asyncapi.json`",
    "`contracts/port-surface.json`",
    "`contracts/error-catalog.json`",
    "Authorization",
    "X-Request-Id",
    "X-Trace-Id",
    "X-Correlation-Id",
    "X-Schema-Version",
    "Idempotency-Key",
    "X-Lark-Request-Timestamp",
    "X-Lark-Request-Nonce",
    "X-Lark-Signature",
    "ETag",
    "X-World-Revision",
    "Location",
    "Retry-After",
    "X-Stream-Protocol-Version",
    "Sec-WebSocket-Protocol",
  ]) {
    assert.ok(guide.includes(requiredText), `guide omits required authority/protocol text ${requiredText}`);
  }
  assert.match(guide, /receiveFeishuWebhook[\s\S]{0,300}不要求 Service Bearer/u);
  assert.match(guide, /全部 6 个 Feishu `POST`[\s\S]{0,250}`Idempotency-Key`/u);
  assert.match(guide, /byte-equivalent body/u);
  assert.match(guide, /EXECUTE_AGENT_TURN[\s\S]{0,160}command\.links\.run/u);
  assert.doesNotMatch(guide, /resource_url 获取[^\n]*Run/u);

  assert.doesNotMatch(
    guide,
    /\b(?:GET|POST|PUT|PATCH|DELETE)\s+\/api\//u,
    "guide must not publish a legacy /api route",
  );
}

test("independent interface guide exactly covers OpenAPI operations, Ports and realtime frames", () => {
  assertInterfaceGuideAligned(readFileSync(GUIDE_PATH, "utf8"));
});

test("interface guide gate fails loudly when a method/path, Port method or WSS frame drifts", () => {
  const guide = readFileSync(GUIDE_PATH, "utf8");
  assert.throws(
    () => assertInterfaceGuideAligned(guide.replace(
      "`/v1/bootstrap` | `getGameBootstrap`",
      "`/v1/bootstrap-wrong` | `getGameBootstrap`",
    )),
    /method\/path\/success status drifted/u,
  );
  assert.throws(
    () => assertInterfaceGuideAligned(guide.replace(
      "`/v1/bootstrap` | `getGameBootstrap` | 200 |",
      "`/v1/bootstrap` | `getGameBootstrap` | 201 |",
    )),
    /method\/path\/success status drifted/u,
  );
  assert.throws(
    () => assertInterfaceGuideAligned(guide.replace("`compile_and_test`", "`compile-test-missing`")),
    /SandboxPort\.compile_and_test/u,
  );
  assert.throws(
    () => assertInterfaceGuideAligned(guide.replace("`heartbeat_ack`", "`heartbeat-ack-missing`")),
    /heartbeat_ack frame/u,
  );
  assert.throws(
    () => assertInterfaceGuideAligned(guide.replace("`world.committed`", "`world-committed-missing`")),
    /world\.committed event/u,
  );
});
