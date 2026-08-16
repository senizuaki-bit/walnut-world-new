import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { PROJECT_ROOT } from "../scripts/validate-contracts.mjs";

function json(path) {
  return JSON.parse(readFileSync(resolve(PROJECT_ROOT, path), "utf8"));
}

test("student bootstrap v2 is one closed additive public Godot boundary", () => {
  const schema = json("contracts/schemas/game/student-bootstrap-v2.schema.json");
  const sessionCreateSchema = json(
    "contracts/schemas/game/agent-session-create-request.schema.json",
  );
  const example = json("contracts/examples/game-student-bootstrap-v2.json").value;
  const openApi = json("contracts/openapi/student-bootstrap-v2.openapi.json");
  const operation = openApi.paths["/v1/student-bootstrap"].get;

  assert.equal(operation.operationId, "getStudentBootstrap");
  assert.equal(operation["x-godot-operation"], "get_student_bootstrap");
  assert.equal(schema.additionalProperties, false);
  assert.deepEqual(schema.required, [
    "request_context", "api_version", "contract_version", "server_time", "actor",
    "content", "capabilities", "session", "build", "activation", "world",
  ]);
  assert.equal(schema.properties.api_version.const, "1.1.0");
  assert.equal(schema.properties.contract_version.const, "0.4.0");
  assert.equal(schema.properties.build.properties.max_source_files.const, 32);
  assert.equal(schema.properties.build.properties.max_source_bytes.const, 1048576);
  assert.equal(example.session.create_request.world_id, example.world.world_id);
  assert.equal(
    example.session.create_request.expected_world_revision,
    example.world.revision,
  );
  assert.deepEqual(
    Object.keys(example.session.create_request).sort(),
    Object.keys(sessionCreateSchema.properties).sort(),
  );
  assert.equal(example.session.teaching_spec_version, "agent-teaching-v1");
  assert.equal(Object.hasOwn(example.session.create_request, "teaching_spec_version"), false);
  assert.equal(example.activation.scope.world_id, example.world.world_id);
  assert.equal(
    example.activation.scope.agent_profile_id,
    example.session.create_request.agent_profile_id,
  );
  assert.equal(example.activation.active.registry_revision, example.activation.registry_revision);

  const gateway = readFileSync(resolve(PROJECT_ROOT, "clients/godot/agent_api_gateway.gd"), "utf8");
  const transport = readFileSync(
    resolve(PROJECT_ROOT, "clients/godot/http_agent_api_transport.gd"),
    "utf8",
  );
  const types = readFileSync(resolve(PROJECT_ROOT, "src/student-bootstrap.d.ts"), "utf8");
  assert.match(gateway, /func get_student_bootstrap\(attempt_context: Dictionary\)/u);
  assert.match(gateway, /validate_student_bootstrap_v2/u);
  assert.match(transport, /"get_student_bootstrap":\s+path = "\/v1\/student-bootstrap"/u);
  assert.match(types, /interface StudentBootstrapV2/u);
  assert.match(types, /readonly contract_version: "0\.4\.0"/u);
  assert.match(types, /readonly http_world_recovery: boolean/u);
  assert.match(types, /readonly locale: string/u);
  assert.match(types, /readonly expected_world_revision: number/u);
  assert.match(types, /interface StudentBootstrapSession[\s\S]*readonly teaching_spec_version: string/u);
});
