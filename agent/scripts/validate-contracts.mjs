import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { isIP } from "node:net";
import { dirname, extname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import {
  assertProductExampleRelationships,
  assertProductExampleSemantics,
} from "./product-experience-invariants.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
export const PROJECT_ROOT = resolve(SCRIPT_DIR, "..");
const CONTRACT_ROOT = resolve(PROJECT_ROOT, "contracts");
const HTTP_METHODS = new Set(["get", "post", "put", "patch", "delete"]);

function invariant(condition, message) {
  if (!condition) throw new Error(message);
}

function walkFiles(directory) {
  const output = [];
  for (const name of readdirSync(directory)) {
    const path = resolve(directory, name);
    if (statSync(path).isDirectory()) output.push(...walkFiles(path));
    else output.push(path);
  }
  return output;
}

function displayPath(path) {
  return relative(PROJECT_ROOT, path).split(sep).join("/");
}

class DuplicateJsonKeyError extends SyntaxError {}

function jsonPointerToken(value) {
  return value.replaceAll("~", "~0").replaceAll("/", "~1");
}

function jsonLocation(source, offset) {
  let line = 1;
  let column = 1;
  for (let index = 0; index < offset; index += 1) {
    if (source[index] === "\r") {
      line += 1;
      column = 1;
      if (source[index + 1] === "\n" && index + 1 < offset) index += 1;
    } else if (source[index] === "\n") {
      line += 1;
      column = 1;
    } else {
      column += 1;
    }
  }
  return `line ${line}, column ${column}, offset ${offset}`;
}

function skipJsonWhitespace(source, start) {
  let index = start;
  while (source[index] === " "
    || source[index] === "\t"
    || source[index] === "\r"
    || source[index] === "\n") index += 1;
  return index;
}

function scanJsonString(source, start) {
  let index = start + 1;
  while (index < source.length) {
    if (source[index] === "\\") index += 2;
    else if (source[index] === "\"") return index + 1;
    else index += 1;
  }
  throw new SyntaxError(`strict JSON scanner reached an unterminated string at offset ${start}`);
}

function scanJsonNumber(source, start) {
  let index = start;
  if (source[index] === "-") index += 1;
  if (source[index] === "0") index += 1;
  else while (source[index] >= "0" && source[index] <= "9") index += 1;
  if (source[index] === ".") {
    index += 1;
    while (source[index] >= "0" && source[index] <= "9") index += 1;
  }
  if (source[index] === "e" || source[index] === "E") {
    index += 1;
    if (source[index] === "+" || source[index] === "-") index += 1;
    while (source[index] >= "0" && source[index] <= "9") index += 1;
  }
  return index;
}

function assertNoDuplicateJsonKeys(source) {
  function scanValue(start, pointer) {
    const index = skipJsonWhitespace(source, start);
    if (source[index] === "{") return scanObject(index, pointer);
    if (source[index] === "[") return scanArray(index, pointer);
    if (source[index] === "\"") return scanJsonString(source, index);
    if (source.startsWith("true", index)) return index + 4;
    if (source.startsWith("false", index)) return index + 5;
    if (source.startsWith("null", index)) return index + 4;
    return scanJsonNumber(source, index);
  }

  function scanObject(start, pointer) {
    const seen = new Map();
    let index = skipJsonWhitespace(source, start + 1);
    if (source[index] === "}") return index + 1;

    while (index < source.length) {
      const keyOffset = index;
      const keyEnd = scanJsonString(source, keyOffset);
      const key = JSON.parse(source.slice(keyOffset, keyEnd));
      const propertyPointer = `${pointer}/${jsonPointerToken(key)}`;
      const firstOffset = seen.get(key);
      if (firstOffset !== undefined) {
        throw new DuplicateJsonKeyError(
          `duplicate object key ${JSON.stringify(key)} at ${propertyPointer} `
          + `(${jsonLocation(source, keyOffset)}); first declared at ${jsonLocation(source, firstOffset)}`,
        );
      }
      seen.set(key, keyOffset);

      index = skipJsonWhitespace(source, keyEnd) + 1;
      index = scanValue(index, propertyPointer);
      index = skipJsonWhitespace(source, index);
      if (source[index] === "}") return index + 1;
      index = skipJsonWhitespace(source, index + 1);
    }
    throw new SyntaxError(`strict JSON scanner reached an unterminated object at ${pointer}`);
  }

  function scanArray(start, pointer) {
    let index = skipJsonWhitespace(source, start + 1);
    if (source[index] === "]") return index + 1;
    let itemIndex = 0;
    while (index < source.length) {
      index = scanValue(index, `${pointer}/${itemIndex}`);
      index = skipJsonWhitespace(source, index);
      if (source[index] === "]") return index + 1;
      index = skipJsonWhitespace(source, index + 1);
      itemIndex += 1;
    }
    throw new SyntaxError(`strict JSON scanner reached an unterminated array at ${pointer}`);
  }

  const end = skipJsonWhitespace(source, scanValue(0, "#"));
  if (end !== source.length) {
    throw new SyntaxError(`strict JSON scanner found trailing content at ${jsonLocation(source, end)}`);
  }
}

export function parseJsonStrict(source) {
  const value = JSON.parse(source);
  if (typeof source === "string") assertNoDuplicateJsonKeys(source);
  return value;
}

function parseJson(path) {
  try {
    return parseJsonStrict(readFileSync(path, "utf8"));
  } catch (error) {
    if (error instanceof DuplicateJsonKeyError) {
      throw new Error(`${displayPath(path)} contains ${error.message}`);
    }
    throw new Error(`${displayPath(path)} is not valid JSON: ${error.message}`);
  }
}

function jsonPointer(document, fragment, sourceLabel) {
  if (!fragment || fragment === "#") return document;
  invariant(fragment.startsWith("#/"), `${sourceLabel} uses unsupported JSON pointer ${fragment}`);
  let current = document;
  for (const encoded of fragment.slice(2).split("/")) {
    const key = decodeURIComponent(encoded).replaceAll("~1", "/").replaceAll("~0", "~");
    invariant(current !== null && typeof current === "object" && key in current,
      `${sourceLabel} cannot resolve JSON pointer ${fragment}`);
    current = current[key];
  }
  return current;
}

export function loadDocuments() {
  const jsonFiles = walkFiles(CONTRACT_ROOT).filter((path) => extname(path) === ".json");
  const documents = new Map(jsonFiles.map((path) => [path, parseJson(path)]));
  return { jsonFiles, documents };
}

export function resolveReference(sourceFile, reference, documents) {
  invariant(typeof reference === "string" && reference.length > 0,
    `${displayPath(sourceFile)} contains an empty $ref`);
  invariant(!/^https?:\/\//u.test(reference),
    `${displayPath(sourceFile)} contains a network $ref; contracts must be self-contained: ${reference}`);

  const hashIndex = reference.indexOf("#");
  const filePart = hashIndex >= 0 ? reference.slice(0, hashIndex) : reference;
  const fragment = hashIndex >= 0 ? reference.slice(hashIndex) : "";
  const targetFile = filePart ? resolve(dirname(sourceFile), filePart) : sourceFile;
  const relativeTarget = relative(PROJECT_ROOT, targetFile);
  invariant(!relativeTarget.startsWith(`..${sep}`) && !isAbsolute(relativeTarget),
    `${displayPath(sourceFile)} references a file outside the package: ${reference}`);
  invariant(existsSync(targetFile), `${displayPath(sourceFile)} has missing $ref target: ${reference}`);
  const targetDocument = documents.get(targetFile) ?? parseJson(targetFile);
  return { targetFile, value: jsonPointer(targetDocument, fragment, `${displayPath(sourceFile)} $ref ${reference}`) };
}

function visit(value, visitor, pointer = "#") {
  if (value === null || typeof value !== "object") return;
  visitor(value, pointer);
  if (Array.isArray(value)) {
    value.forEach((item, index) => visit(item, visitor, `${pointer}/${index}`));
  } else {
    for (const [key, item] of Object.entries(value)) visit(item, visitor, `${pointer}/${key}`);
  }
}

function parameterNames(operation, pathItem, sourceFile, documents) {
  return [...(pathItem.parameters ?? []), ...(operation.parameters ?? [])]
    .map((parameter) => parameter?.$ref
      ? resolveReference(sourceFile, parameter.$ref, documents).value
      : parameter)
    .filter((parameter) => parameter && typeof parameter === "object")
    .map((parameter) => `${String(parameter.in).toLowerCase()}:${String(parameter.name).toLowerCase()}`);
}

function validateOpenApi(path, document, operationIds, documents) {
  invariant(/^3\.1\./u.test(document.openapi ?? ""), `${displayPath(path)} must use OpenAPI 3.1.x`);
  invariant(document.info?.title && document.info?.version, `${displayPath(path)} is missing info.title/version`);
  invariant(document.paths && typeof document.paths === "object", `${displayPath(path)} is missing paths`);
  let operationCount = 0;

  for (const [route, pathItem] of Object.entries(document.paths)) {
    invariant(route.startsWith("/"), `${displayPath(path)} has invalid path ${route}`);
    for (const [method, operation] of Object.entries(pathItem)) {
      if (!HTTP_METHODS.has(method.toLowerCase())) continue;
      operationCount += 1;
      invariant(operation.operationId, `${displayPath(path)} ${method.toUpperCase()} ${route} has no operationId`);
      invariant(!operationIds.has(operation.operationId), `duplicate operationId ${operation.operationId}`);
      operationIds.add(operation.operationId);
      invariant(operation.responses && Object.keys(operation.responses).length > 0,
        `${operation.operationId} has no responses`);
      const names = parameterNames(operation, pathItem, path, documents);
      invariant(names.includes("header:x-request-id"), `${operation.operationId} must require X-Request-Id`);
      invariant(names.includes("header:x-schema-version"), `${operation.operationId} must require X-Schema-Version`);
      if (method.toLowerCase() !== "get") {
        invariant(names.includes("header:idempotency-key"), `${operation.operationId} must require Idempotency-Key`);
      }
      const responseCodes = Object.keys(operation.responses);
      invariant(responseCodes.some((code) => code === "default" || /^4|^5/u.test(code)),
        `${operation.operationId} must declare an error response`);
      invariant(responseCodes.includes("409"),
        `${operation.operationId} must declare X-Schema-Version conflicts as 409`);
      if (displayPath(path).endsWith("feishu-integration.openapi.json")) {
        invariant(names.includes("header:x-trace-id"), `${operation.operationId} must require X-Trace-Id`);
      }
    }
  }
  return operationCount;
}

function validateAsyncApi(path, document) {
  invariant(/^3\./u.test(document.asyncapi ?? ""), `${displayPath(path)} must use AsyncAPI 3.x`);
  invariant(document.info?.title && document.info?.version, `${displayPath(path)} is missing info.title/version`);
  invariant(document.channels && Object.keys(document.channels).length > 0, `${displayPath(path)} has no channels`);
  const eventTypes = new Set();
  visit(document, (node) => {
    if (typeof node.const === "string"
      && /^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$/u.test(node.const)) eventTypes.add(node.const);
    if (typeof node["x-event-type"] === "string") eventTypes.add(node["x-event-type"]);
  });
  return eventTypes;
}

function validateErrorCatalog(path, catalog) {
  invariant(catalog.catalog_version && Array.isArray(catalog.errors), `${displayPath(path)} has invalid shape`);
  const codes = new Set();
  for (const error of catalog.errors) {
    invariant(/^[A-Z][A-Z0-9_]+$/u.test(error.code ?? ""), `invalid error code ${error.code}`);
    invariant(!codes.has(error.code), `duplicate error code ${error.code}`);
    codes.add(error.code);
    invariant(Number.isInteger(error.http_status) && error.http_status >= 400 && error.http_status <= 599,
      `${error.code} has invalid http_status`);
    invariant(typeof error.retryable === "boolean", `${error.code} has no retryable flag`);
    invariant(typeof error.user_message_key === "string" && error.user_message_key.length > 0,
      `${error.code} has no user_message_key`);
  }
  return codes;
}

function validateErrorSchemaBinding(catalog, errorSchema, errorCodeSchema, auditSchema) {
  const catalogCodes = new Set(catalog.errors.map((entry) => entry.code));
  const enumCodes = new Set(errorCodeSchema?.enum ?? []);
  invariant(catalogCodes.size === enumCodes.size
    && [...catalogCodes].every((code) => enumCodes.has(code)),
  "common/error-code.schema.json enum must exactly match error-catalog.json");
  invariant(errorSchema?.properties?.code?.$ref === "./error-code.schema.json",
    "common/error.schema.json code must reference the shared error-code schema");
  invariant(auditSchema?.properties?.error_code?.oneOf?.some(
    (branch) => branch.$ref === "./error-code.schema.json"),
  "common/audit-record.schema.json error_code must reference the shared error-code schema");
  const definitionCodes = new Set(Object.keys(errorSchema.$defs ?? {}));
  invariant(catalogCodes.size === definitionCodes.size
    && [...catalogCodes].every((code) => definitionCodes.has(code)),
  "common/error.schema.json $defs must exactly match error-catalog.json");

  const branchRefs = new Set((errorSchema.allOf?.[0]?.oneOf ?? [])
    .map((branch) => branch.$ref?.replace("#/$defs/", "")));
  invariant(catalogCodes.size === branchRefs.size
    && [...catalogCodes].every((code) => branchRefs.has(code)),
  "common/error.schema.json oneOf must bind every and only catalog error");

  for (const entry of catalog.errors) {
    const properties = errorSchema.$defs[entry.code]?.properties ?? {};
    for (const field of ["code", "category", "retryable", "user_message_key"]) {
      invariant(Object.is(properties[field]?.const, entry[field]),
        `error schema tuple drift for ${entry.code}.${field}`);
    }
  }
}

function typeMatches(value, type) {
  switch (type) {
    case "null": return value === null;
    case "object": return value !== null && typeof value === "object" && !Array.isArray(value);
    case "array": return Array.isArray(value);
    case "integer": return Number.isInteger(value);
    case "number": return typeof value === "number" && Number.isFinite(value);
    default: return typeof value === type;
  }
}

function jsonDataEqual(left, right) {
  if (left === right) return true;
  if (left === null || right === null || typeof left !== typeof right) return false;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => jsonDataEqual(item, right[index]));
  }
  if (typeof left === "object") {
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    return leftKeys.length === rightKeys.length
      && leftKeys.every((key) => Object.hasOwn(right, key) && jsonDataEqual(left[key], right[key]));
  }
  return false;
}

function hasUniqueJsonItems(value) {
  for (let index = 0; index < value.length; index += 1) {
    for (let previous = 0; previous < index; previous += 1) {
      if (jsonDataEqual(value[index], value[previous])) return false;
    }
  }
  return true;
}

const UNRESERVED = /^[A-Za-z0-9._~-]$/u;
const SUB_DELIMITERS = new Set(["!", "$", "&", "'", "(", ")", "*", "+", ",", ";", "="]);

function isRfc3986Component(value, extraCharacters = "", allowPercentEncoding = true) {
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (character === "%") {
      if (!allowPercentEncoding
        || index + 2 >= value.length
        || !/^[0-9A-Fa-f]{2}$/u.test(value.slice(index + 1, index + 3))) return false;
      index += 2;
    } else if (!UNRESERVED.test(character)
      && !SUB_DELIMITERS.has(character)
      && !extraCharacters.includes(character)) return false;
  }
  return true;
}

function isRfc3986IpLiteral(value) {
  if (isIP(value) === 6) return true;
  const match = /^[vV][0-9A-Fa-f]+\.(.+)$/u.exec(value);
  return Boolean(match?.[1]) && isRfc3986Component(match[1], ":", false);
}

function isRfc3986Authority(value) {
  const firstAt = value.indexOf("@");
  const lastAt = value.lastIndexOf("@");
  if (firstAt !== lastAt) return false;
  let hostAndPort = value;
  if (lastAt >= 0) {
    if (!isRfc3986Component(value.slice(0, lastAt), ":")) return false;
    hostAndPort = value.slice(lastAt + 1);
  }

  if (hostAndPort.startsWith("[")) {
    const closingBracket = hostAndPort.indexOf("]");
    if (closingBracket < 0 || !isRfc3986IpLiteral(hostAndPort.slice(1, closingBracket))) return false;
    const suffix = hostAndPort.slice(closingBracket + 1);
    return suffix === "" || /^:[0-9]*$/u.test(suffix);
  }
  if (hostAndPort.includes("[") || hostAndPort.includes("]")) return false;

  const firstColon = hostAndPort.indexOf(":");
  const lastColon = hostAndPort.lastIndexOf(":");
  if (firstColon !== lastColon) return false;
  const host = lastColon >= 0 ? hostAndPort.slice(0, lastColon) : hostAndPort;
  const port = lastColon >= 0 ? hostAndPort.slice(lastColon + 1) : undefined;
  return isRfc3986Component(host) && (port === undefined || /^[0-9]*$/u.test(port));
}

function isRfc3986Path(value, absoluteUri) {
  if (value.startsWith("//")) {
    const pathIndex = value.indexOf("/", 2);
    const authority = pathIndex < 0 ? value.slice(2) : value.slice(2, pathIndex);
    const path = pathIndex < 0 ? "" : value.slice(pathIndex);
    return isRfc3986Authority(authority) && isRfc3986Component(path, ":@/");
  }
  if (value === "" || value.startsWith("/")) return isRfc3986Component(value, ":@/");
  if (!isRfc3986Component(value, ":@/")) return false;
  if (absoluteUri) return true;
  const firstSegment = value.slice(0, value.indexOf("/") < 0 ? value.length : value.indexOf("/"));
  return firstSegment.length > 0 && isRfc3986Component(firstSegment, "@");
}

function isRfc3986Reference(value, requireScheme) {
  if (typeof value !== "string" || /[^\u0000-\u007f]/u.test(value)) return false;
  const hashIndex = value.indexOf("#");
  if (hashIndex >= 0 && value.indexOf("#", hashIndex + 1) >= 0) return false;
  const fragment = hashIndex < 0 ? undefined : value.slice(hashIndex + 1);
  const withoutFragment = hashIndex < 0 ? value : value.slice(0, hashIndex);
  const queryIndex = withoutFragment.indexOf("?");
  const query = queryIndex < 0 ? undefined : withoutFragment.slice(queryIndex + 1);
  const pathAndAuthority = queryIndex < 0 ? withoutFragment : withoutFragment.slice(0, queryIndex);
  if ((query !== undefined && !isRfc3986Component(query, ":@/?"))
    || (fragment !== undefined && !isRfc3986Component(fragment, ":@/?"))) return false;

  const scheme = /^([A-Za-z][A-Za-z0-9+.-]*):/u.exec(pathAndAuthority);
  if (requireScheme && !scheme) return false;
  const path = scheme ? pathAndAuthority.slice(scheme[0].length) : pathAndAuthority;
  return isRfc3986Path(path, Boolean(scheme));
}

function isRfc3339DateTime(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?([Zz]|[+-](\d{2}):(\d{2}))$/u.exec(value);
  if (!match) return false;

  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  if (year < 1 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) {
    return false;
  }
  const daysInMonth = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)) daysInMonth[1] = 29;
  if (day < 1 || day > daysInMonth[month - 1]) return false;

  if (match[8] !== undefined && (Number(match[8]) > 23 || Number(match[9]) > 59)) return false;
  return true;
}

export function assertSchema(value, schema, schemaFile, documents, location = "$") {
  if (schema.$ref) {
    const resolved = resolveReference(schemaFile, schema.$ref, documents);
    return assertSchema(value, resolved.value, resolved.targetFile, documents, location);
  }
  if (schema.allOf) schema.allOf.forEach((candidate) => assertSchema(value, candidate, schemaFile, documents, location));
  if (schema.oneOf) {
    let valid = 0;
    const branchErrors = [];
    for (const candidate of schema.oneOf) {
      try {
        assertSchema(value, candidate, schemaFile, documents, location);
        valid += 1;
      } catch (error) {
        branchErrors.push(error instanceof Error ? error.message : String(error));
      }
    }
    invariant(valid === 1,
      `${location} must match exactly one oneOf branch, matched ${valid}; branches: ${branchErrors.join(" | ")}`);
  }
  if (schema.anyOf) {
    const branchErrors = [];
    let valid = false;
    for (const candidate of schema.anyOf) {
      try {
        assertSchema(value, candidate, schemaFile, documents, location);
        valid = true;
        break;
      } catch (error) {
        branchErrors.push(error instanceof Error ? error.message : String(error));
      }
    }
    invariant(valid, `${location} does not match any anyOf branch: ${branchErrors.join(" | ")}`);
  }
  if (schema.not) {
    let matchedForbiddenSchema = false;
    try {
      assertSchema(value, schema.not, schemaFile, documents, location);
      matchedForbiddenSchema = true;
    } catch (error) {
      if (!(error instanceof Error)) throw error;
    }
    invariant(!matchedForbiddenSchema, `${location} matches a forbidden not schema`);
  }
  if (schema.if) {
    let conditionMatches = false;
    try {
      assertSchema(value, schema.if, schemaFile, documents, location);
      conditionMatches = true;
    } catch (error) {
      if (!(error instanceof Error)) throw error;
    }
    if (conditionMatches && schema.then) assertSchema(value, schema.then, schemaFile, documents, location);
    if (!conditionMatches && schema.else) assertSchema(value, schema.else, schemaFile, documents, location);
  }
  if (Object.hasOwn(schema, "const")) invariant(jsonDataEqual(value, schema.const), `${location} must equal ${JSON.stringify(schema.const)}`);
  if (schema.enum) invariant(schema.enum.some((candidate) => jsonDataEqual(candidate, value)), `${location} is not in enum`);
  if (schema.type) {
    const allowed = Array.isArray(schema.type) ? schema.type : [schema.type];
    invariant(allowed.some((type) => typeMatches(value, type)), `${location} must be ${allowed.join("|")}`);
  }

  if (typeof value === "string") {
    const codePointLength = Array.from(value).length;
    if (schema.minLength !== undefined) invariant(codePointLength >= schema.minLength, `${location} is too short`);
    if (schema.maxLength !== undefined) invariant(codePointLength <= schema.maxLength, `${location} is too long`);
    if (schema.pattern) invariant(new RegExp(schema.pattern, "u").test(value), `${location} does not match ${schema.pattern}`);
    if (schema.format === "date-time") {
      invariant(isRfc3339DateTime(value), `${location} is not RFC3339 date-time`);
    }
    if (schema.format === "uri") {
      invariant(isRfc3986Reference(value, true), `${location} is not an absolute URI`);
    }
    if (schema.format === "uri-reference") {
      invariant(isRfc3986Reference(value, false), `${location} is not a URI reference`);
    }
    if (schema.format === "uuid") {
      invariant(/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu.test(value),
        `${location} is not a UUID`);
    }
  }
  if (typeof value === "number") {
    if (schema.minimum !== undefined) invariant(value >= schema.minimum, `${location} is below minimum`);
    if (schema.maximum !== undefined) invariant(value <= schema.maximum, `${location} is above maximum`);
  }
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined) invariant(value.length >= schema.minItems, `${location} has too few items`);
    if (schema.maxItems !== undefined) invariant(value.length <= schema.maxItems, `${location} has too many items`);
    if (schema.uniqueItems) invariant(hasUniqueJsonItems(value),
      `${location} must contain unique items`);
    if (schema["x-invariants"]?.includes(
      "evidence_refs contains at most one immutable reference for each evidence_id",
    )) {
      const evidenceIds = value.map((item) => item?.evidence_id);
      invariant(new Set(evidenceIds).size === evidenceIds.length,
        `${location} must contain unique evidence_id values`);
    }
    if (schema.items) value.forEach((item, index) => assertSchema(item, schema.items, schemaFile, documents, `${location}[${index}]`));
  }
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    if (schema.minProperties !== undefined) invariant(Object.keys(value).length >= schema.minProperties,
      `${location} has too few properties`);
    if (schema.maxProperties !== undefined) invariant(Object.keys(value).length <= schema.maxProperties,
      `${location} has too many properties`);
    for (const required of schema.required ?? []) invariant(Object.hasOwn(value, required), `${location}.${required} is required`);
    for (const [trigger, dependencies] of Object.entries(schema.dependentRequired ?? {})) {
      if (!Object.hasOwn(value, trigger)) continue;
      for (const dependent of dependencies) invariant(Object.hasOwn(value, dependent),
        `${location}.${dependent} is required when ${trigger} is present`);
    }
    for (const [key, item] of Object.entries(value)) {
      if (schema.properties?.[key]) assertSchema(item, schema.properties[key], schemaFile, documents, `${location}.${key}`);
      else if (schema.additionalProperties === false) throw new Error(`${location}.${key} is not allowed`);
      else if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
        assertSchema(item, schema.additionalProperties, schemaFile, documents, `${location}.${key}`);
      }
    }
  }
}

function validateExamples(documents) {
  const exampleFiles = [...documents.keys()].filter((path) => displayPath(path).startsWith("contracts/examples/"));
  const validatedExamples = [];
  let count = 0;
  for (const path of exampleFiles) {
    const wrapper = documents.get(path);
    invariant(typeof wrapper.schema_ref === "string" && Object.hasOwn(wrapper, "value"),
      `${displayPath(path)} must contain schema_ref and value`);
    const resolved = resolveReference(path, wrapper.schema_ref, documents);
    assertSchema(wrapper.value, resolved.value, resolved.targetFile, documents, `${displayPath(path)}.value`);
    assertProductExampleSemantics(wrapper.value, resolved.targetFile);
    if (displayPath(resolved.targetFile) === "contracts/schemas/game/student-bootstrap-v2.schema.json") {
      const value = wrapper.value;
      const sessionCreateRequest = resolveReference(
        resolved.targetFile,
        "agent-session-create-request.schema.json",
        documents,
      );
      assertSchema(
        value.session.create_request,
        sessionCreateRequest.value,
        sessionCreateRequest.targetFile,
        documents,
        `${displayPath(path)}.value.session.create_request`,
      );
      invariant(JSON.stringify(value.request_context.actor) === JSON.stringify(value.actor),
        `${displayPath(path)} request_context.actor must equal actor`);
      invariant(JSON.stringify(value.request_context.content_ref) === JSON.stringify(value.content),
        `${displayPath(path)} request_context.content_ref must equal content`);
      invariant(value.actor.actor_type === "student"
        && value.session.create_request.learner_id === value.actor.actor_id,
      `${displayPath(path)} learner authority must equal the student actor`);
      invariant(value.session.create_request.world_id === value.world.world_id
        && value.activation.scope.world_id === value.world.world_id,
      `${displayPath(path)} session and activation must target the bootstrap world`);
      invariant(value.activation.scope.agent_profile_id
        === value.session.create_request.agent_profile_id,
      `${displayPath(path)} session and activation must target the same agent profile`);
      invariant(JSON.stringify(value.session.create_request.content) === JSON.stringify(value.content),
        `${displayPath(path)} create_request.content must equal content`);
      invariant(value.session.create_request.expected_world_revision === value.world.revision,
        `${displayPath(path)} create_request.expected_world_revision must equal world.revision`);
      invariant(value.activation.active === null
        || value.activation.active.registry_revision === value.activation.registry_revision,
      `${displayPath(path)} active skill must match registry_revision`);
      invariant(value.world.snapshot_url === `/v1/worlds/${value.world.world_id}/snapshot`
        && value.world.events_url === `/v1/worlds/${value.world.world_id}/events`,
      `${displayPath(path)} recovery URLs must identify the bootstrap world`);
    }
    validatedExamples.push({ path, schemaFile: resolved.targetFile, value: wrapper.value });
    count += 1;
  }
  assertProductExampleRelationships(validatedExamples);
  return count;
}

export function validateContracts() {
  const { jsonFiles, documents } = loadDocuments();
  const ids = new Map();
  let refCount = 0;
  for (const [path, document] of documents) {
    if (document.$id) {
      invariant(!ids.has(document.$id), `duplicate $id ${document.$id}`);
      ids.set(document.$id, path);
    }
    visit(document, (node) => {
      if (node.$ref) {
        resolveReference(path, node.$ref, documents);
        refCount += 1;
      }
    });
  }

  const operationIds = new Set();
  const eventTypes = new Set();
  for (const [path, document] of documents) {
    if (document.openapi) validateOpenApi(path, document, operationIds, documents);
    if (document.asyncapi) {
      for (const eventType of validateAsyncApi(path, document)) eventTypes.add(eventType);
    }
  }
  const errorCatalogPath = resolve(CONTRACT_ROOT, "error-catalog.json");
  const errorCatalog = documents.get(errorCatalogPath);
  const errors = validateErrorCatalog(errorCatalogPath, errorCatalog);
  const errorSchemaPath = resolve(CONTRACT_ROOT, "schemas/common/error.schema.json");
  const errorCodeSchemaPath = resolve(CONTRACT_ROOT, "schemas/common/error-code.schema.json");
  const auditSchemaPath = resolve(CONTRACT_ROOT, "schemas/common/audit-record.schema.json");
  validateErrorSchemaBinding(
    errorCatalog,
    documents.get(errorSchemaPath),
    documents.get(errorCodeSchemaPath),
    documents.get(auditSchemaPath),
  );
  const exampleCount = validateExamples(documents);

  return {
    files: jsonFiles.length,
    refs: refCount,
    operations: operationIds.size,
    events: eventTypes.size,
    errors: errors.size,
    examples: exampleCount,
  };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const summary = validateContracts();
    console.log(`AGENT_CONTRACTS_VALIDATION_OK files=${summary.files} refs=${summary.refs} operations=${summary.operations} events=${summary.events} errors=${summary.errors} examples=${summary.examples}`);
  } catch (error) {
    console.error(`AGENT_CONTRACTS_VALIDATION_FAILED ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  }
}
