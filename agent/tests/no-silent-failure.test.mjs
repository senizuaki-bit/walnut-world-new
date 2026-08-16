import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { PROJECT_ROOT } from "../scripts/validate-contracts.mjs";

function json(path) {
  return JSON.parse(readFileSync(resolve(PROJECT_ROOT, path), "utf8"));
}

test("every catalog error has stable transport and retry semantics", () => {
  const catalog = json("contracts/error-catalog.json");
  const seen = new Set();
  for (const error of catalog.errors) {
    assert.equal(seen.has(error.code), false, `duplicate error code ${error.code}`);
    seen.add(error.code);
    assert.ok(error.http_status >= 400 && error.http_status <= 599);
    assert.equal(typeof error.retryable, "boolean");
    assert.match(error.user_message_key, /^[a-z][a-z0-9_.-]+$/u);
  }
  for (const required of [
    "IDEMPOTENCY_KEY_REUSED", "WORLD_REVISION_CONFLICT", "SANDBOX_RESOURCE_LIMIT",
    "WORLD_RULE_REJECTED", "UNKNOWN_COMMIT_STATE", "INVARIANT_VIOLATION",
  ]) {
    assert.ok(seen.has(required), `missing non-silent error ${required}`);
  }
});

test("error response cannot hide failure inside a success payload", () => {
  const schema = json("contracts/schemas/common/error-response.schema.json");
  assert.deepEqual(schema.properties.data, { type: "null" });
  assert.ok(schema.required.includes("error"));
  assert.ok(schema.properties.status.enum.every((status) => status !== "APPLIED"));
});

test("Feishu contract cannot expose real-time world mutation routes", () => {
  const openApiFiles = readdirSync(resolve(PROJECT_ROOT, "contracts/openapi"));
  assert.ok(
    openApiFiles.includes("feishu-integration.openapi.json"),
    "the Feishu OpenAPI contract is mandatory for the no-silent-failure gate",
  );
  const path = resolve(PROJECT_ROOT, "contracts/openapi/feishu-integration.openapi.json");
  const contract = JSON.parse(readFileSync(path, "utf8"));
  for (const route of Object.keys(contract.paths ?? {})) {
    assert.doesNotMatch(route, /world.*(?:apply|command|mutation)|skill.*activate/iu);
  }
});

test("domain declarations do not import infrastructure adapters", () => {
  const domainDir = resolve(PROJECT_ROOT, "src/domain");
  for (const name of readdirSync(domainDir).filter((item) => item.endsWith(".d.ts"))) {
    const source = readFileSync(resolve(domainDir, name), "utf8");
    assert.doesNotMatch(source, /(?:http|fetch|postgres|database|sdk|adapter)/iu,
      `${name} leaks infrastructure into the domain`);
  }
});
