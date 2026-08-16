import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { PROJECT_ROOT } from "../scripts/validate-contracts.mjs";

function source(path) {
  return readFileSync(resolve(PROJECT_ROOT, path), "utf8");
}

test("umbrella verification includes both Ruff lint and format gates", () => {
  const verify = source("scripts/verify-all.ps1");
  assert.match(verify, /"Ruff lint"[\s\S]*?"ruff", "check", "python", "tests"/u);
  assert.match(
    verify,
    /"Ruff format check"[\s\S]*?"ruff",[\s\S]*?"format",[\s\S]*?"--check",[\s\S]*?"python",[\s\S]*?"tests"/u,
  );
});

test("Node release runner emits TAP and rejects every skipped test", () => {
  const runner = source("scripts/run-node-tests.mjs");
  assert.match(runner, /--test-reporter=tap/u);
  assert.match(runner, /matchAll\(\/\^# skipped \(\\d\+\)/u);
  assert.match(runner, /release gates allow zero skips/u);
});

test("Node contract tests declare no static or dynamic skip semantics", () => {
  const testRoot = resolve(PROJECT_ROOT, "tests");
  const skipPattern = /(?:\b(?:test|it|describe)\.skip\s*\(|[{,]\s*skip\s*:|\.skip\s*\()/u;
  for (const name of readdirSync(testRoot).filter((item) => item.endsWith(".test.mjs"))) {
    assert.doesNotMatch(source(`tests/${name}`), skipPattern, `${name} declares a skipped test`);
  }
});

test("wheel gate builds and imports from a clean dependency-checked venv", () => {
  const packageGate = source("scripts/test-python-package.ps1");
  assert.match(packageGate, /-m venv \$venvRoot/u);
  assert.match(packageGate, /\$venvPython -I -m pip install/u);
  assert.match(packageGate, /\$venvPython -I -m pip check/u);
  assert.match(packageGate, /\$venvPython -I -c/u);
  assert.doesNotMatch(packageGate, /--target|--no-deps/u);
});

test("true Provider public student chain uses HTTP and run_forever workers", () => {
  const roleLive = source("tests/test_agent_backend_role_live_e2e.py");
  const publicRoleLive = source("tests/test_agent_backend_public_role_live_e2e.py");
  assert.match(publicRoleLive, /composition\.worker\.run_forever\(agent_stop\)/u);
  assert.match(publicRoleLive, /composition\.learner_worker\.run_forever\(learner_stop\)/u);
  assert.match(
    publicRoleLive,
    /composition\.student_chain_worker\.run_forever\(control_stop\)/u,
  );
  assert.match(publicRoleLive, /target="\/v1\/agent-sessions"/u);
  assert.match(publicRoleLive, /"PUT",\s*target/u);
  assert.match(publicRoleLive, /\/product-experience\/v1\/sessions\/.*\/skill-drafts\//u);
  assert.match(publicRoleLive, /target="\/v1\/skill-builds"/u);
  assert.match(publicRoleLive, /\/skill-versions\/.*\/activations/u);
  assert.match(
    roleLive,
    /"POST",\s*f"\/v1\/agent-sessions\/\{getattr\(self, 'session_id', SESSION_ID\)\}\/turns"/u,
  );
  assert.match(roleLive, /f"\/v1\/commands\/\{command_id\}"/u);
  assert.match(roleLive, /f"\/v1\/evidence\/\{reference\['evidence_id'\]\}"/u);
  assert.match(roleLive, /f"\/v1\/worlds\/\{WORLD_ID\}\/snapshot"/u);
  assert.match(roleLive, /agent-interactions\?after_sequence=\{after_sequence\}&limit=1/u);
  for (const liveSource of [roleLive, publicRoleLive]) {
    assert.doesNotMatch(liveSource, /GameEvent\s*\(/u);
    assert.doesNotMatch(liveSource, /invocations\.invoke\s*\(/u);
    assert.doesNotMatch(liveSource, /process_claimed_event\s*\(/u);
  }
});
