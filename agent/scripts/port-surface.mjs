import { readFileSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
export const PROJECT_ROOT = resolve(SCRIPT_DIRECTORY, "..");
export const PORT_SURFACE_PATH = resolve(PROJECT_ROOT, "contracts/port-surface.json");

const REQUIRED_IDEMPOTENCY_SCOPES = {
  CommandStorePort: {
    components: [
      "context.actor.tenant_id",
      "context.actor.actor_id",
      "operation",
      "idempotency_key",
    ],
    actor_boundary: "required",
    hash_field: "command.request_sha256",
  },
  OutboxPort: {
    components: [
      "message.operation_context.actor.tenant_id",
      "message.destination",
      "message.idempotency_key",
    ],
    actor_boundary: "service_delivery_exception",
    hash_field: "message.payload_sha256",
  },
};

function invariant(condition, message) {
  if (!condition) throw new Error(message);
}

function normalizeType(value) {
  return value
    .replace(/\s+/gu, " ")
    .trim()
    .replace(/\s*([\[\](){},:<>|])\s*/gu, "$1")
    .replace(/\s*->\s*/gu, "->");
}

function splitTopLevel(value, delimiter = ",") {
  const parts = [];
  let start = 0;
  const stack = [];
  let quote = null;
  let escaped = false;
  const pairs = { "(": ")", "[": "]", "{": "}", "<": ">" };
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (quote !== null) {
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "\"" || character === "'") {
      quote = character;
    } else if (pairs[character]) {
      stack.push(pairs[character]);
    } else if (stack.at(-1) === character) {
      stack.pop();
    } else if (character === delimiter && stack.length === 0) {
      parts.push(value.slice(start, index));
      start = index + 1;
    }
  }
  parts.push(value.slice(start));
  return parts.map((part) => part.trim()).filter(Boolean);
}

function findBalancedEnd(text, openIndex, openCharacter, closeCharacter) {
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let index = openIndex; index < text.length; index += 1) {
    const character = text[index];
    if (quote !== null) {
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "\"" || character === "'") quote = character;
    else if (character === openCharacter) depth += 1;
    else if (character === closeCharacter) {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  throw new Error(`unclosed ${openCharacter} at offset ${openIndex}`);
}

function extractPythonClass(source, className) {
  const marker = new RegExp(`^class ${className}\\(Protocol\\):\\s*$`, "mu").exec(source);
  invariant(marker, `Python Protocol ${className} is missing`);
  const start = marker.index + marker[0].length;
  const nextTopLevel = /^class |^__all__\s*=/gmu;
  nextTopLevel.lastIndex = start;
  const end = nextTopLevel.exec(source)?.index ?? source.length;
  return source.slice(start, end);
}

function extractPythonMethodSignature(classBody, methodName) {
  const marker = new RegExp(`^    (async )?def ${methodName}\\s*\\(`, "mu").exec(classBody);
  invariant(marker, `Python method ${methodName} is missing`);
  const start = marker.index + 4;
  const open = classBody.indexOf("(", start);
  const close = findBalancedEnd(classBody, open, "(", ")");
  const tail = classBody.slice(close + 1);
  const returnMatch = /^\s*->\s*([^:]+):/u.exec(tail);
  invariant(returnMatch, `Python method ${methodName} has no parseable return annotation`);
  return {
    is_async: marker[1] === "async ",
    parametersText: classBody.slice(open + 1, close),
    returnType: normalizeType(returnMatch[1]),
  };
}

function parseParameters(parametersText, language, methodName) {
  return splitTopLevel(parametersText)
    .map((parameter) => normalizeType(parameter))
    .filter((parameter) => !(language === "python" && parameter === "self"))
    .map((parameter) => {
      const colon = parameter.indexOf(":");
      invariant(colon !== -1, `${language} ${methodName} parameter ${parameter} has no type`);
      const rawName = parameter.slice(0, colon).replace(/\?$/u, "");
      return {
        name: rawName,
        type: parameter.slice(colon + 1),
      };
    });
}

export function collectPythonPort(source, portName) {
  const body = extractPythonClass(source, portName);
  const methodNames = [...body.matchAll(/^    (?:async )?def ([A-Za-z_][A-Za-z0-9_]*)\s*\(/gmu)]
    .map((match) => match[1]);
  return Object.fromEntries(methodNames.map((methodName) => {
    const signature = extractPythonMethodSignature(body, methodName);
    return [methodName, {
      is_async: signature.is_async,
      parameters: parseParameters(signature.parametersText, "python", methodName),
      return_type: signature.returnType,
    }];
  }));
}

function extractTypeScriptInterface(source, interfaceName) {
  const marker = `export interface ${interfaceName}`;
  const start = source.indexOf(marker);
  invariant(start !== -1, `TypeScript interface ${interfaceName} is missing`);
  const open = source.indexOf("{", start);
  const close = findBalancedEnd(source, open, "{", "}");
  return source.slice(open + 1, close);
}

function extractTypeScriptMethodSignature(interfaceBody, methodName) {
  const marker = new RegExp(`^\\s{2}${methodName}(?:\\s*<|\\s*\\()`, "mu").exec(interfaceBody);
  invariant(marker, `TypeScript method ${methodName} is missing`);
  const start = marker.index + marker[0].search(/\S/u);
  const open = interfaceBody.indexOf("(", start);
  invariant(open !== -1, `TypeScript method ${methodName} has no parameter list`);
  const nameEnd = start + methodName.length;
  const genericText = interfaceBody.slice(nameEnd, open).trim();
  const close = findBalancedEnd(interfaceBody, open, "(", ")");
  const tail = interfaceBody.slice(close + 1);
  const returnMatch = /^\s*:\s*([^;]+);/u.exec(tail);
  invariant(returnMatch, `TypeScript method ${methodName} has no parseable return type`);
  const returnType = normalizeType(returnMatch[1]);
  const asyncMatch = /^AsyncResult<([\s\S]+)>$/u.exec(returnType);
  invariant(asyncMatch, `TypeScript ${methodName} must return AsyncResult<Success,Error>`);
  const resultArguments = splitTopLevel(asyncMatch[1]).map(normalizeType);
  invariant(
    resultArguments.length === 2,
    `TypeScript ${methodName} AsyncResult must declare success and error types`,
  );
  return {
    parametersText: interfaceBody.slice(open + 1, close),
    genericText,
    returnType,
    successType: resultArguments[0],
    errorType: resultArguments[1],
  };
}

export function collectTypeScriptPort(source, portName) {
  const body = extractTypeScriptInterface(source, portName);
  const methodNames = [...body.matchAll(/^\s{2}([A-Za-z][A-Za-z0-9]*)\s*(?:<|\()/gmu)]
    .map((match) => match[1]);
  return Object.fromEntries(methodNames.map((methodName) => {
    const signature = extractTypeScriptMethodSignature(body, methodName);
    return [methodName, {
      type_parameters: signature.genericText === ""
        ? []
        : splitTopLevel(signature.genericText.slice(1, -1)).map(normalizeType),
      parameters: parseParameters(signature.parametersText, "typescript", methodName),
      return_type: signature.returnType,
      async_result: true,
      success_type: signature.successType,
      error_type: signature.errorType,
    }];
  }));
}

function deepEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function assertIdempotencyScopeSemantics(manifest) {
  for (const [portName, expected] of Object.entries(REQUIRED_IDEMPOTENCY_SCOPES)) {
    const port = manifest.ports.find((candidate) => candidate.python === portName);
    invariant(port, `${portName} is missing from the port surface`);
    invariant(
      deepEqual(port.idempotency_scope, expected),
      `${portName} idempotency_scope drifted; expected ${JSON.stringify(expected)}, `
      + `actual ${JSON.stringify(port.idempotency_scope)}`,
    );
  }
}

function displayPath(path) {
  return relative(PROJECT_ROOT, path).replaceAll("\\", "/");
}

export function discoverPythonProtocolPorts(source) {
  return [
    ...source.matchAll(
      /^class ([A-Za-z][A-Za-z0-9]*Port)\([^)]*\bProtocol\b[^)]*\):\s*$/gmu,
    ),
  ]
    .map((match) => match[1])
    .sort();
}

export function discoverTypeScriptPortExports(
  indexPath,
  readSource = (path) => readFileSync(path, "utf8"),
) {
  const indexSource = readSource(indexPath);
  const exportPaths = [...indexSource.matchAll(/^export \* from "(\.\/[^"]+\.js)";\s*$/gmu)]
    .map((match) => match[1]);
  const discovered = new Map();
  const declarationPaths = [indexPath, ...exportPaths.map((exportPath) => resolve(
    dirname(indexPath),
    exportPath.replace(/\.js$/u, ".d.ts"),
  ))];
  for (const declarationPath of declarationPaths) {
    const declarationSource = readSource(declarationPath);
    const interfaceNames = [
      ...declarationSource.matchAll(
        /^export interface ([A-Za-z][A-Za-z0-9]*Port)(?:\s|<|\{)/gmu,
      ),
    ].map((match) => match[1]);
    for (const interfaceName of interfaceNames) {
      invariant(
        !discovered.has(interfaceName),
        `TypeScript Port ${interfaceName} is exported by multiple files`,
      );
      discovered.set(interfaceName, {
        kind: "interface",
        typescript_file: displayPath(declarationPath),
      });
    }
    const aliases = [
      ...declarationSource.matchAll(
        /^export type ([A-Za-z][A-Za-z0-9]*Port)\s*=\s*([A-Za-z][A-Za-z0-9]*Port)\s*;/gmu,
      ),
    ];
    for (const alias of aliases) {
      invariant(
        !discovered.has(alias[1]),
        `TypeScript Port ${alias[1]} is exported by multiple declarations`,
      );
      discovered.set(alias[1], {
        kind: "alias",
        target: alias[2],
        typescript_file: displayPath(declarationPath),
      });
    }
  }
  return discovered;
}

function assertUnique(values, label) {
  const duplicates = values.filter((value, index) => values.indexOf(value) !== index);
  invariant(duplicates.length === 0, `${label} contains duplicates: ${[...new Set(duplicates)]}`);
}

function assertExactSet(actualValues, expectedValues, label) {
  assertUnique(expectedValues, `${label} manifest set`);
  const actual = [...actualValues].sort();
  const expected = [...expectedValues].sort();
  if (deepEqual(actual, expected)) return;
  const missingFromManifest = actual.filter((value) => !expected.includes(value));
  const missingFromSource = expected.filter((value) => !actual.includes(value));
  throw new Error(
    `${label} set differs from the manifest; `
    + `missing from manifest: ${missingFromManifest.join(", ") || "(none)"}; `
    + `missing from source: ${missingFromSource.join(", ") || "(none)"}`,
  );
}

export function assertCompletePortInventory(
  manifest,
  readSource = (path) => readFileSync(path, "utf8"),
) {
  invariant(typeof manifest.python_file === "string", "port surface python_file is missing");
  invariant(
    typeof manifest.typescript_index_file === "string",
    "port surface typescript_index_file is missing",
  );
  const pythonPath = resolve(PROJECT_ROOT, manifest.python_file);
  const discoveredPython = discoverPythonProtocolPorts(readSource(pythonPath));
  assertExactSet(
    discoveredPython,
    manifest.ports.map((port) => port.python),
    "Python Protocol Port",
  );

  const indexPath = resolve(PROJECT_ROOT, manifest.typescript_index_file);
  const discoveredTypeScript = discoverTypeScriptPortExports(indexPath, readSource);
  const discoveredInterfaces = [...discoveredTypeScript]
    .filter(([, declaration]) => declaration.kind === "interface")
    .map(([name]) => name);
  assertExactSet(
    discoveredInterfaces,
    manifest.ports.map((port) => port.typescript),
    "TypeScript exported Port interface",
  );
  for (const port of manifest.ports) {
    invariant(
      discoveredTypeScript.get(port.typescript).typescript_file === port.typescript_file,
      `${port.typescript} is exported from `
      + `${discoveredTypeScript.get(port.typescript).typescript_file}, `
      + `not manifest file ${port.typescript_file}`,
    );
  }
  invariant(
    Array.isArray(manifest.typescript_port_aliases),
    "port surface typescript_port_aliases is missing",
  );
  const discoveredAliases = [...discoveredTypeScript]
    .filter(([, declaration]) => declaration.kind === "alias")
    .map(([name]) => name);
  assertExactSet(
    discoveredAliases,
    manifest.typescript_port_aliases.map((alias) => alias.name),
    "TypeScript exported Port alias",
  );
  for (const alias of manifest.typescript_port_aliases) {
    const declaration = discoveredTypeScript.get(alias.name);
    invariant(
      declaration.target === alias.target
        && declaration.typescript_file === alias.typescript_file,
      `${alias.name} alias drifted; expected ${alias.target} in ${alias.typescript_file}, `
      + `actual ${declaration.target} in ${declaration.typescript_file}`,
    );
  }
}

export function buildPortSurface(manifest, readSource = (path) => readFileSync(path, "utf8")) {
  assertCompletePortInventory(manifest, readSource);
  assertIdempotencyScopeSemantics(manifest);
  const pythonPath = resolve(PROJECT_ROOT, manifest.python_file);
  const pythonSource = readSource(pythonPath);
  const output = structuredClone(manifest);
  output.schema_version = "2.0.0";
  for (const port of output.ports) {
    const pythonMethods = collectPythonPort(pythonSource, port.python);
    const typescriptPath = resolve(PROJECT_ROOT, port.typescript_file);
    const typescriptMethods = collectTypeScriptPort(readSource(typescriptPath), port.typescript);
    const expectedPythonNames = port.methods.map((method) => method.python).sort();
    const expectedTypeScriptNames = port.methods.map((method) => method.typescript).sort();
    invariant(
      deepEqual(Object.keys(pythonMethods).sort(), expectedPythonNames),
      `${port.python} method set differs from the manifest mapping`,
    );
    invariant(
      deepEqual(Object.keys(typescriptMethods).sort(), expectedTypeScriptNames),
      `${port.typescript} method set differs from the manifest mapping`,
    );
    for (const method of port.methods) {
      delete method.python_parameters;
      delete method.typescript_parameters;
      method.python_contract = pythonMethods[method.python];
      method.typescript_contract = typescriptMethods[method.typescript];
    }
  }
  return output;
}

export function verifyPortSurface(manifest, readSource = (path) => readFileSync(path, "utf8")) {
  invariant(manifest.schema_version === "2.0.0", "port surface schema_version must be 2.0.0");
  invariant(typeof manifest.python_file === "string", "port surface python_file is missing");
  invariant(
    typeof manifest.typescript_index_file === "string",
    "port surface typescript_index_file is missing",
  );
  const actual = buildPortSurface(manifest, readSource);
  const failures = [];
  for (let portIndex = 0; portIndex < manifest.ports.length; portIndex += 1) {
    const expectedPort = manifest.ports[portIndex];
    const actualPort = actual.ports[portIndex];
    for (let methodIndex = 0; methodIndex < expectedPort.methods.length; methodIndex += 1) {
      const expectedMethod = expectedPort.methods[methodIndex];
      const actualMethod = actualPort.methods[methodIndex];
      for (const language of ["python", "typescript"]) {
        const key = `${language}_contract`;
        if (!deepEqual(expectedMethod[key], actualMethod[key])) {
          failures.push(
            `${expectedPort[language]}.${expectedMethod[language]} ${key} drifted\n`
            + `expected ${JSON.stringify(expectedMethod[key])}\n`
            + `actual   ${JSON.stringify(actualMethod[key])}`,
          );
        }
      }
    }
  }
  if (failures.length > 0) throw new Error(failures.join("\n\n"));
  return actual;
}

function loadManifest() {
  return JSON.parse(readFileSync(PORT_SURFACE_PATH, "utf8"));
}

function run() {
  const mode = process.argv[2] ?? "--check";
  const manifest = loadManifest();
  if (mode === "--write") {
    const updated = buildPortSurface(manifest);
    writeFileSync(PORT_SURFACE_PATH, `${JSON.stringify(updated, null, 2)}\n`, "utf8");
    console.log(`updated ${displayPath(PORT_SURFACE_PATH)}`);
  } else if (mode === "--check") {
    verifyPortSurface(manifest);
    console.log("PORT_SURFACE_SIGNATURES_OK");
  } else {
    throw new Error(`unknown mode ${mode}; expected --check or --write`);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    run();
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  }
}
