import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { assertSchema, loadDocuments, PROJECT_ROOT } from "../scripts/validate-contracts.mjs";
const root = path.resolve(import.meta.dirname, "..");
const read = (relative) => JSON.parse(fs.readFileSync(path.join(root, relative), "utf8"));

test("v0.6 INT2 capabilities example is closed and valid", () => {
  const { documents } = loadDocuments();
  const schemaPath = path.resolve(
    PROJECT_ROOT,
    "contracts/schemas/product-experience/int2-capabilities.schema.json",
  );
  const schema = documents.get(schemaPath);
  const wrapper = read("contracts/examples/product-int2-capabilities.json");
  const example = wrapper.value;
  assert.equal(
    wrapper.schema_ref,
    "../schemas/product-experience/int2-capabilities.schema.json",
  );
  assertSchema(example, schema, schemaPath, documents);
  for (const field of [
    "request_mode",
    "selection_target",
    "agent_role",
    "scenario",
    "required_hint_level",
    "operation",
    "target",
    "max_files",
    "max_operations",
    "requires_failed_evidence",
    "cas_required",
    "requires_student_confirmation",
    "auto_build",
    "auto_activate",
    "auto_run",
  ]) {
    assert.ok(Object.hasOwn(example.skill_patch_constraints, field));
  }
  assert.equal(example.skill_patch_enabled, false);
  assert.equal(example.skill_patch_constraints.selection_target, "FAILED_INTERACTION");
  const constraints = schema.properties.skill_patch_constraints;
  assert.equal(constraints.additionalProperties, false);
  assert.equal(constraints.properties.auto_build.const, false);
  assert.equal(constraints.properties.selection_target.const, "FAILED_INTERACTION");
});

test("v0.6 capability OpenAPI is one GET-only additive route", () => {
  const openapi = read("contracts/openapi/int2-product-capabilities.openapi.json");
  assert.equal(openapi.info.version, "0.6.0");
  assert.deepEqual(Object.keys(openapi.paths), ["/product-experience/v1/capabilities"]);
  const route = openapi.paths["/product-experience/v1/capabilities"];
  assert.deepEqual(Object.keys(route), ["get"]);
  assert.equal(route.get.operationId, "getInt2Capabilities");
  assert.equal(
    route.get.responses["200"].content["application/json"].schema.$ref,
    "../schemas/product-experience/int2-capabilities.schema.json",
  );
});
