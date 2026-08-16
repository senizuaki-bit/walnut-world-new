import assert from "node:assert/strict";
import test from "node:test";

import { loadDocuments, parseJsonStrict } from "../scripts/validate-contracts.mjs";

test("strict contract JSON parser preserves JSON.parse values for valid documents", () => {
  const source = String.raw`{
    "message": "commas, colons: and braces { } [ ] are data; escaped quote: \"",
    "numbers": [-0, 1.25e+3],
    "same_key_in_distinct_objects": [{"value": 1}, {"value": 2}],
    "unicode": {"a": "\\u0061", "music": "𝄞"}
  }`;

  assert.deepEqual(parseJsonStrict(source), JSON.parse(source));
  assert.equal(parseJsonStrict("null"), null);
  assert.equal(parseJsonStrict("  true\r\n"), true);
});

test("strict contract JSON parser rejects literal duplicate keys with a location", () => {
  assert.throws(
    () => parseJsonStrict('{"outer":{"safe":1,"safe":2}}'),
    (error) => {
      assert.match(error.message, /duplicate object key "safe"/u);
      assert.match(error.message, /#\/outer\/safe/u);
      assert.match(error.message, /line 1, column \d+, offset \d+/u);
      assert.match(error.message, /first declared at line 1, column \d+, offset \d+/u);
      return true;
    },
  );
});

test("strict contract JSON parser decodes escaped keys before duplicate comparison", () => {
  assert.throws(
    () => parseJsonStrict(String.raw`{"a": 1, "\u0061": 2}`),
    /duplicate object key "a" at #\/a/u,
  );
  assert.throws(
    () => parseJsonStrict(String.raw`{"outer": {"a/b~c": 1, "a\/b\u007ec": 2}}`),
    /duplicate object key "a\/b~c" at #\/outer\/a~1b~0c/u,
  );
  assert.throws(
    () => parseJsonStrict(String.raw`{"𝄞": 1, "\ud834\udd1e": 2}`),
    /duplicate object key "𝄞"/u,
  );
});

test("strict contract JSON parser does not confuse string punctuation with structure", () => {
  assert.doesNotThrow(() => parseJsonStrict(String.raw`{
    "text": "fake key: \\\"x\\\": 1, }, [ and \\\\u0061",
    "nested": {"x": 1},
    "array": ["{\\\"x\\\":1,\\\"x\\\":2}"]
  }`));
});

test("strict parser retains JSON.parse rejection semantics for malformed JSON", () => {
  for (const source of ['{"a":}', '{"a":1,}', '[1,,2]', '"unterminated']) {
    assert.throws(() => JSON.parse(source));
    assert.throws(() => parseJsonStrict(source));
  }
});

test("all checked-in contract JSON documents pass the strict loader", () => {
  const { jsonFiles, documents } = loadDocuments();
  assert.ok(jsonFiles.length > 0);
  assert.equal(documents.size, jsonFiles.length);
});
