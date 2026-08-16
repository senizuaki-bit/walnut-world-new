import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { canonicalJsonSha256V1, canonicalJsonV1 } from "../src/canonical-json.mjs";
import { PROJECT_ROOT } from "../scripts/validate-contracts.mjs";

const PYTHON_EXE = process.env.YAYA_PYTHON_EXE ?? "python";

function json(path) {
  return JSON.parse(readFileSync(resolve(PROJECT_ROOT, path), "utf8"));
}

test("YAYA_CANONICAL_JSON_V1 is stable across JavaScript and independent Python", () => {
  const specification = json("contracts/canonical-json-v1.json");
  assert.equal(specification.canonicalization_id, "YAYA_CANONICAL_JSON_V1");
  const vector = specification.vectors[0];
  assert.equal(canonicalJsonV1(vector.value), vector.canonical_utf8);
  assert.equal(canonicalJsonSha256V1(vector.value), vector.sha256);

  const python = spawnSync(PYTHON_EXE, ["-X", "utf8", "-c", String.raw`
import hashlib
import json
import sys

vector = json.load(sys.stdin)
canonical = json.dumps(
    vector["value"],
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
)
print(canonical)
print(hashlib.sha256(canonical.encode("utf-8")).hexdigest())
`], {
    cwd: PROJECT_ROOT,
    encoding: "utf8",
    input: JSON.stringify(vector),
  });
  assert.equal(python.status, 0, python.stderr || python.stdout);
  const [pythonCanonical, pythonSha256] = python.stdout.trim().split(/\r?\n/u);
  assert.equal(pythonCanonical, vector.canonical_utf8);
  assert.equal(pythonSha256, vector.sha256);
});

test("canonical evidence hashes ignore key insertion order and reject ambiguous numbers", () => {
  const evidence = json("contracts/examples/game-evidence.json").value;
  const reorderedPayload = Object.fromEntries(Object.entries(evidence.payload).reverse());
  assert.equal(canonicalJsonSha256V1(reorderedPayload), evidence.integrity.payload_sha256);
  assert.equal(evidence.evidence_ref.sha256, evidence.integrity.payload_sha256);
  for (const value of [1.5, Number.MAX_SAFE_INTEGER + 1, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => canonicalJsonV1({ value }), /safe integer/u);
  }
  assert.throws(() => canonicalJsonV1({ value: undefined }), /non-JSON/u);
});

test("canonical JSON rejects ill-formed Unicode while preserving valid scalar pairs", () => {
  assert.equal(canonicalJsonV1({ emoji: "\u{1F331}" }), '{"emoji":"\u{1F331}"}');
  for (const value of ["\uD800", "\uDC00", `prefix\uD800suffix`]) {
    assert.throws(() => canonicalJsonV1({ value }), /Unicode scalar/u);
  }
  assert.throws(
    () => canonicalJsonV1({ ["\uD800"]: "invalid key" }),
    /Unicode scalar/u,
  );
});
