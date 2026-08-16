import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function text(path) {
  return readFileSync(resolve(ROOT, path), "utf8");
}

function normalized(name) {
  return name.toLowerCase().replaceAll(/[._-]+/gu, "-");
}

function pinnedRequirements(source) {
  const pins = new Map();
  for (const match of source.matchAll(/^([A-Za-z0-9_.-]+)==([^\s\\]+).*$/gmu)) {
    pins.set(normalized(match[1]), match[2]);
  }
  return pins;
}

function quotedPins(source) {
  const pins = new Map();
  for (const match of source.matchAll(/["']([A-Za-z0-9_.-]+)==([^"']+)["']/gu)) {
    pins.set(normalized(match[1]), match[2]);
  }
  return pins;
}

test("the Windows Python CI lock covers every direct dependency with hashes", () => {
  const directSource = text("requirements-test.txt");
  const lockSource = text("requirements-ci-win-py312.txt");
  const direct = pinnedRequirements(directSource);
  const locked = pinnedRequirements(lockSource);

  assert.ok(lockSource.includes("--require-hashes"));
  assert.ok(lockSource.includes("--only-binary=:all:"));
  assert.ok(locked.size > direct.size, "the CI lock must include transitive dependencies");
  for (const [name, version] of direct) {
    assert.equal(locked.get(name), version, `${name} must have the same direct and CI version`);
  }

  const requirementBlocks = lockSource
    .split(/\r?\n(?=[A-Za-z0-9_.-]+==)/u)
    .filter((block) => /^[A-Za-z0-9_.-]+==/u.test(block));
  assert.equal(requirementBlocks.length, locked.size);
  for (const block of requirementBlocks) {
    assert.match(block, /--hash=sha256:[a-f0-9]{64}(?:\s|$)/u);
  }
});

test("GitHub Actions installs only the hash-locked Python CI environment", () => {
  const workflow = text(".github/workflows/contracts.yml");
  assert.match(
    workflow,
    /pip install[^\r\n]*--require-hashes[^\r\n]*-r requirements-ci-win-py312\.txt/u,
  );
  assert.doesNotMatch(workflow, /pip install[^\r\n]*-r requirements-test\.txt/u);
});

test("the Python test extra includes every schema format validator", () => {
  const direct = pinnedRequirements(text("requirements-test.txt"));
  const testExtra = quotedPins(text("pyproject.toml"));

  for (const name of ["jsonschema", "rfc3339-validator", "rfc3986-validator"]) {
    assert.equal(testExtra.get(name), direct.get(name), `${name} must match requirements-test.txt`);
  }
});
