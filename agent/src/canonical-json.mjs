import { createHash } from "node:crypto";

function compareUnicodeScalars(left, right) {
  const leftScalars = Array.from(left);
  const rightScalars = Array.from(right);
  const count = Math.min(leftScalars.length, rightScalars.length);
  for (let index = 0; index < count; index += 1) {
    const difference = leftScalars[index].codePointAt(0) - rightScalars[index].codePointAt(0);
    if (difference !== 0) return difference;
  }
  return leftScalars.length - rightScalars.length;
}

function assertWellFormedUnicode(value, path) {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 0xD800 && codeUnit <= 0xDBFF) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xDC00 && next <= 0xDFFF)) {
        throw new TypeError(`${path} must contain only Unicode scalar values`);
      }
      index += 1;
    } else if (codeUnit >= 0xDC00 && codeUnit <= 0xDFFF) {
      throw new TypeError(`${path} must contain only Unicode scalar values`);
    }
  }
}

function encodeCanonical(value, path) {
  if (value === null) return "null";
  if (typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "string") {
    assertWellFormedUnicode(value, path);
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw new TypeError(`${path} must contain only safe integer JSON numbers`);
    }
    return Object.is(value, -0) ? "0" : String(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item, index) => encodeCanonical(item, `${path}[${index}]`)).join(",")}]`;
  }
  if (typeof value === "object") {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new TypeError(`${path} must contain only plain JSON objects`);
    }
    const keys = Object.keys(value);
    for (const key of keys) assertWellFormedUnicode(key, `${path} object key`);
    keys.sort(compareUnicodeScalars);
    return `{${keys.map((key) => (
      `${JSON.stringify(key)}:${encodeCanonical(value[key], `${path}.${key}`)}`
    )).join(",")}}`;
  }
  throw new TypeError(`${path} contains a non-JSON value`);
}

/**
 * YAYA_CANONICAL_JSON_V1 is intentionally narrower than arbitrary JSON:
 * evidence payload numbers are safe integers. This makes the exact UTF-8 byte
 * representation reproducible in JavaScript, Python and Godot without relying
 * on implementation-specific floating-point formatting.
 */
export function canonicalJsonV1(value) {
  return encodeCanonical(value, "value");
}

export function canonicalJsonSha256V1(value) {
  return createHash("sha256").update(canonicalJsonV1(value), "utf8").digest("hex");
}
