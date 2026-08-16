import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { PROJECT_ROOT } from "../scripts/validate-contracts.mjs";

function readProjectSource(relativePath) {
  return readFileSync(resolve(PROJECT_ROOT, relativePath), "utf8").replace(/\r\n/gu, "\n");
}

function resolveLocalReference(document, value) {
  if (!value?.$ref?.startsWith("#/")) return value;
  return value.$ref.slice(2).split("/").reduce((current, segment) => current[segment], document);
}

function gameClientOperations() {
  const document = JSON.parse(readProjectSource("contracts/openapi/game-api.openapi.json"));
  const operations = [];
  for (const [path, pathItem] of Object.entries(document.paths)) {
    for (const method of ["get", "post", "put", "patch", "delete"]) {
      const operation = pathItem[method];
      if (!operation) continue;
      const clientOperation = operation["x-godot-operation"];
      assert.match(clientOperation ?? "", /^[a-z][a-z0-9_]{2,63}$/u,
        `${operation.operationId} must publish x-godot-operation`);
      const parameters = [...(pathItem.parameters ?? []), ...(operation.parameters ?? [])]
        .map((parameter) => resolveLocalReference(document, parameter));
      const query = parameters.filter((parameter) => parameter.in === "query")
        .map((parameter) => `${parameter.name}=%s`);
      const template = path.replace(/\{[^}]+\}/gu, "%s")
        + (query.length > 0 ? `?${query.join("&")}` : "");
      operations.push({
        operationId: operation.operationId,
        clientOperation,
        method: method.toUpperCase(),
        template,
      });
    }
  }
  assert.equal(new Set(operations.map((operation) => operation.clientOperation)).size,
    operations.length, "x-godot-operation values must be unique");
  return operations;
}

test("Godot gateway exposes commands without leaking HTTPRequest", () => {
  const source = readProjectSource("clients/godot/agent_api_gateway.gd");
  const executableSource = source.split(/\r?\n/u).filter((line) => !line.trimStart().startsWith("#")).join("\n");
  assert.doesNotMatch(executableSource, /HTTPRequest|HTTPClient/u);
  const operations = gameClientOperations().map((operation) => operation.clientOperation);
  for (const operation of operations) {
    assert.match(source, new RegExp(`func ${operation}\\(`, "u"));
  }
  assert.match(source, /var _transport: AgentApiTransport/u,
    "Gateway must accept only implementations of the declared transport port");
  assert.match(source, /await _transport\.execute\(operation, arguments\)/u,
    "all adapters must be awaited behind one transport port");
  for (const operation of operations) {
    const start = source.indexOf(`func ${operation}(`);
    const end = source.indexOf("\n\n", start);
    assert.match(source.slice(start, end), /return await _dispatch/u,
      `${operation} must not leak an unresolved async transport call`);
  }
  assert.match(source, /result\.size\(\) != 4/u,
    "transport success/failure envelopes must reject ignored fields");
  assert.match(source, /_validate_success_metadata/u,
    "transport status and protocol headers must be validated before returning success");
  assert.match(source, /return _success_result\(result\.status, metadata_validation\.headers, result\.value\)/u,
    "validated success metadata must cross the gateway boundary");
  assert.match(source, /return _failure_result\(result\.status, error_headers_check\.headers, result\.error\)/u,
    "validated server failure metadata must cross the gateway boundary");
  assert.match(source,
    /return \{"ok": true, "status": status, "headers": headers, "value": value\}/u,
    "success results must be strictly closed four-field envelopes");
  assert.match(source,
    /return \{"ok": false, "status": status, "headers": headers, "error": error\}/u,
    "failure results must be strictly closed four-field envelopes");
  assert.match(source, /x-request-id/u);
  assert.match(source, /x-trace-id/u);
  assert.match(source, /retry-after/u);
  assert.match(source, /idempotency-replayed/u,
    "accepted responses must distinguish first acceptance from idempotent replay");
  assert.match(source, /response_identity\.erase\("trace_id"\)/u,
    "replayed receipts may retain the original command trace");
  assert.match(source, /value\.trace_id != attempt_context\.trace_id/u,
    "a first acceptance must bind its command trace to the current HTTP attempt");
  assert.match(source, /result\.status == 429/u,
    "429 failures must require Retry-After before crossing the gateway boundary");
  assert.match(source, /result\.status == 503 and result\.error\.error\.retryable == true/u,
    "retryable 503 failures must require Retry-After before crossing the gateway boundary");
  assert.match(source,
    /var expected_location := "\/v1\/commands\/%s" % result\.error\.command_id/u,
    "UNKNOWN_COMMIT_STATE must derive its reconciliation URL from the validated command_id");
  assert.match(source,
    /error_headers_check\.headers\.get\("location"\) != expected_location/u,
    "UNKNOWN_COMMIT_STATE must fail loudly when Location is missing or mismatched");
  assert.match(source, /etag/u);
  assert.doesNotMatch(source, /floori\(value\.(?:revision|snapshot_revision)\)/u,
    "metadata validation must not coerce malformed revision fields before payload validation");
  assert.match(source, /not value\.has\("revision"\)[\s\S]*typeof\(value\.revision\) != TYPE_INT/u,
    "world snapshot metadata must guard revision presence and type before use");
  assert.match(source, /not value\.has\("snapshot_revision"\)[\s\S]*typeof\(value\.snapshot_revision\) != TYPE_INT/u,
    "world event metadata must guard snapshot_revision presence and type before use");
  const identityStart = source.indexOf("func _validate_identity_headers(");
  const identityEnd = source.indexOf("\n\n", identityStart);
  const identityBlock = source.slice(identityStart, identityEnd);
  for (const [header, field] of [
    ["x-request-id", "request_id"],
    ["x-trace-id", "trace_id"],
    ["x-correlation-id", "correlation_id"],
  ]) {
    assert.match(identityBlock, new RegExp(
      `normalized\\.get\\("${header}"\\) != attempt_context\\.${field}`, "u",
    ), `${header} must echo the current HTTP attempt ${field}`);
  }
  assert.doesNotMatch(source, /func _request_context_matches\(/u,
    "immutable resource origin context must not be equated to the current HTTP attempt");
  const originStart = source.indexOf("func _origin_actor_matches(");
  const originEnd = source.indexOf("\n\n", originStart);
  const originBlock = source.slice(originStart, originEnd);
  assert.match(originBlock, /actual_context\.get\("actor"\) == expected_actor/u,
    "resource bodies must remain bound to the authenticated origin actor");
  assert.doesNotMatch(originBlock, /request_id|trace_id|correlation_id|requested_at|content_ref/u,
    "resource origin validation must not compare immutable origin metadata with current attempt IDs");
  assert.match(source, /\{"origin_actor": request_context\.actor, "command_id": command_id\}/u,
    "resource bodies must still bind their path identity independently of attempt metadata");
  assert.doesNotMatch(source, /func _transport_/u,
    "subclasses must not bypass the public response-validation wrapper");
  for (const operation of [
    "submit_skill_build", "activate_skill_version", "create_agent_session",
    "submit_agent_turn", "upload_client_events",
  ]) {
    const start = source.indexOf(`func ${operation}(`);
    const end = source.indexOf(") -> Dictionary:", start);
    assert.match(source.slice(start, end), /idempotency_key/u,
      `${operation} must expose the required Idempotency-Key explicitly`);
  }
  const worldEventsStart = source.indexOf("func get_world_events(");
  const worldEventsEnd = source.indexOf(") -> Dictionary:", worldEventsStart);
  assert.match(source.slice(worldEventsStart, worldEventsEnd), /limit: int = 100/u,
    "world event pagination must expose the OpenAPI limit parameter");
});

test("Godot UNKNOWN_COMMIT_STATE validation is bidirectional and reconciliation-safe", () => {
  const validator = readProjectSource("clients/godot/contract_validator.gd");
  const gateway = readProjectSource("clients/godot/agent_api_gateway.gd");
  const runner = readProjectSource("clients/godot/contract_test_runner.gd");
  const start = validator.indexOf("static func validate_error_response(");
  const end = validator.indexOf("\n\nstatic func ", start);
  const block = validator.slice(start, end);

  assert.match(block, /var is_unknown_commit: bool = value\.error\.code == "UNKNOWN_COMMIT_STATE"/u);
  assert.match(block, /if not value\.has\("command_id"\)/u,
    "UNKNOWN errors must have an identity that callers can reconcile");
  assert.match(block, /elif is_unknown_commit:/u,
    "non-UNKNOWN statuses must reject UNKNOWN_COMMIT_STATE");
  assert.match(gateway, /"UNKNOWN_COMMIT_STATE Location does not match command_id\."/u);
  assert.match(gateway, /contract_violation\.emit\(error\)/u,
    "gateway contract failures must remain observable instead of being silently downgraded");

  for (const marker of [
    "unknown_error_without_command",
    "unknown_code_with_failed_status",
    "ordinary_code_with_unknown_status",
    "missing_unknown_location",
    "wrong_unknown_location",
    "wrong_unknown_command",
  ]) {
    assert.match(runner, new RegExp(marker, "u"), `Godot runner is missing ${marker}`);
  }
  assert.match(runner,
    /ContractValidator\.validate_error_response\(unknown_error_response\)\.ok/u,
    "Godot runner must retain a valid UNKNOWN reconciliation case");
});

test("Godot HTTP transport is asynchronous, fail-loud, cancellable and operation-complete", () => {
  const source = readProjectSource("clients/godot/http_agent_api_transport.gd");
  const port = readProjectSource("clients/godot/agent_api_transport.gd");
  const runner = readProjectSource("clients/godot/http_transport_test_runner.gd");
  const testScript = readProjectSource("scripts/test-godot-contracts.ps1");
  const executableSource = source.split(/\r?\n/u)
    .filter((line) => !line.trimStart().startsWith("#")).join("\n");

  assert.match(port, /await Engine\.get_main_loop\(\)\.process_frame/u,
    "even the default transport port must expose an awaitable boundary");
  assert.match(source, /HTTPRequest\.new\(\)/u);
  assert.match(source, /_host\.add_child\(pending\.request_node\)/u,
    "each concurrent request needs a scene-tree-owned HTTPRequest");
  assert.match(source, /var result: Dictionary = await pending\.finished/u,
    "HTTP completion must resume through a signal instead of blocking the main thread");
  assert.match(source, /pending\.request_node\.timeout = _timeout_seconds/u);
  assert.match(source, /pending\.request_node\.body_size_limit = _max_response_bytes/u);
  assert.match(source, /pending\.request_node\.accept_gzip = false/u,
    "response limits must not be bypassed by implicit decompression");
  assert.match(source, /pending\.request_node\.cancel_request\(\)/u);
  assert.match(source, /_pending\.size\(\) >= _max_in_flight/u);
  assert.match(source, /LOCAL_TRANSPORT_BUSY/u);
  assert.match(source, /LOCAL_TRANSPORT_TIMEOUT/u);
  assert.match(source, /LOCAL_TRANSPORT_CANCELLED/u);
  assert.match(source, /LOCAL_TRANSPORT_NETWORK_ERROR/u);
  assert.match(source, /LOCAL_TRANSPORT_RESPONSE_TOO_LARGE/u);
  assert.match(source, /LOCAL_TRANSPORT_JSON_INVALID/u);
  assert.match(source, /LOCAL_TRANSPORT_JSON_SHAPE_INVALID/u);
  assert.match(source, /LOCAL_TRANSPORT_JSON_DUPLICATE_KEY/u);
  assert.match(source, /response_text\.to_utf8_buffer\(\) != body/u,
    "invalid UTF-8 must not be silently replaced before JSON parsing");
  assert.match(source, /StrictJsonObjectScanner\.new\(\)\.inspect\(response_text\)/u,
    "duplicate JSON members must be rejected before Gateway validation");
  assert.ok(
    source.indexOf("StrictJsonObjectScanner.new().inspect(response_text)")
      < source.indexOf("json.parse(response_text)"),
    "ill-formed Unicode escapes must be rejected before Godot can replace them",
  );
  assert.match(source, /strict_json_result\.ill_formed_unicode_found/u);
  assert.match(source, /var normalized_json_data: Dictionary = _normalize_json_integers\(json\.data\)/u,
    "parsed JSON numbers must be normalized before strict Gateway validation");
  assert.match(source,
    /func _normalize_json_integers\([\s\S]*TYPE_FLOAT:[\s\S]*TYPE_ARRAY:[\s\S]*TYPE_DICTIONARY:/u,
    "integer-valued floats must be normalized recursively through arrays and objects");
  assert.match(source, /typeof\(value\) == TYPE_STRING and not value\.is_empty\(\)/u,
    "query arguments must support non-empty strings such as ContentUnit content_hash");
  assert.match(runner, /float_command_result\.value\.result\.previous_revision/u);
  assert.match(runner, /transport\.call\("_query_argument", \{"content_hash": content_hash\}/u);
  assert.match(source, /LOCAL_TRANSPORT_UNEXPECTED_STATUS/u);
  assert.doesNotMatch(source, /OS\.delay_msec|wait_to_finish|while\s+.*HTTPClient/u,
    "the HTTP adapter must never spin or sleep on the SceneTree thread");
  assert.doesNotMatch(executableSource, /ContractValidator/u,
    "the transport must not duplicate or bypass Gateway contract validation");

  for (const header of [
    "Authorization: Bearer", "X-Request-Id:", "X-Trace-Id:",
    "X-Correlation-Id:", "X-Schema-Version:", "Idempotency-Key:",
    "Content-Type: application/json; charset=utf-8",
  ]) {
    assert.ok(source.includes(header), `missing HTTP request header mapping: ${header}`);
  }

  const operationContracts = gameClientOperations();
  const methodNames = {
    POST: "HTTPClient.METHOD_POST",
    PUT: "HTTPClient.METHOD_PUT",
    PATCH: "HTTPClient.METHOD_PATCH",
    DELETE: "HTTPClient.METHOD_DELETE",
  };
  assert.match(source, /var method := HTTPClient\.METHOD_GET/u,
    "GET must be the explicit default request method");
  for (const { clientOperation, method, template, operationId } of operationContracts) {
    const arm = source.indexOf(`\t\t"${clientOperation}":`);
    assert.ok(arm >= 0, `${clientOperation} HTTP mapping is missing`);
    const nextArm = source.indexOf("\n\t\t\"", arm + 4);
    const block = source.slice(arm, nextArm < 0 ? source.length : nextArm);
    if (method === "GET") {
      assert.doesNotMatch(block, /method = HTTPClient\.METHOD_/u,
        `${clientOperation} must retain the declared GET method from ${operationId}`);
    } else {
      assert.ok(block.includes(methodNames[method]),
        `${clientOperation} HTTP method drifted from ${operationId}`);
    }
    assert.ok(block.includes(`"${template}"`),
      `${clientOperation} HTTP path drifted from ${operationId}`);
    assert.ok(runner.includes(`"${clientOperation}"`),
      `${clientOperation} is not exercised by the real Godot transport runner`);
  }

  assert.match(source,
    /"ok": true,[\s\S]*"status": response_code,[\s\S]*"headers": normalized_headers_result\.headers,[\s\S]*"value": normalized_json_data/u,
    "successful HTTP responses must use the strict four-field transport envelope");
  assert.match(source,
    /"ok": false,[\s\S]*"status": response_code,[\s\S]*"headers": normalized_headers_result\.headers,[\s\S]*"error": normalized_json_data/u,
    "server errors must use the strict four-field transport envelope");
  assert.match(runner, /AGENT_GODOT_HTTP_TRANSPORT_TEST_OK/u);
  for (const behavior of [
    "A real HTTP call blocked the SceneTree main loop",
    "LOCAL_TRANSPORT_JSON_INVALID", "LOCAL_TRANSPORT_JSON_DUPLICATE_KEY", "LOCAL_TRANSPORT_TIMEOUT",
    "LOCAL_TRANSPORT_CANCELLED", "LOCAL_TRANSPORT_BUSY", "LOCAL_TRANSPORT_RESPONSE_TOO_LARGE",
  ]) {
    assert.ok(runner.includes(behavior), `real Godot HTTP test does not cover ${behavior}`);
  }
  assert.match(testScript, /res:\/\/http_transport_test_runner\.gd/u,
    "the real HTTP transport runner must be part of npm run test:godot");
});

test("Godot Bearer transport target policy is derived from authoritative OpenAPI", () => {
  const source = readProjectSource("clients/godot/http_agent_api_transport.gd");
  const runner = readProjectSource("clients/godot/http_transport_test_runner.gd");
  const game = JSON.parse(readProjectSource("contracts/openapi/game-api.openapi.json"));
  const productionSchemes = [...new Set(game.servers.map((server) => new URL(server.url).protocol))];
  assert.deepEqual(productionSchemes, ["https:"],
    "every authoritative production Game API server must use HTTPS");

  const development = game.components.securitySchemes.bearerAuth["x-development-profile"];
  const mockServer = new URL(development.server);
  assert.equal(mockServer.protocol, "http:");
  assert.equal(mockServer.pathname, "/", "the plaintext Mock must be rooted at the origin");
  assert.equal(development.production_allowed, false);
  assert.ok(development.loopback_host_aliases.includes(mockServer.hostname),
    "the canonical Mock hostname must be an explicit loopback alias");
  assert.deepEqual(new Set(development.loopback_host_aliases), new Set(["127.0.0.1", "localhost"]),
    "plaintext aliases must remain explicit names, never a DNS or CIDR trust rule");

  const scalarConstant = (name) => {
    const match = new RegExp(`^const ${name} := (?:"([^"]+)"|([0-9]+))$`, "mu").exec(source);
    assert.ok(match, `Godot transport constant ${name} is missing`);
    return match[1] ?? Number(match[2]);
  };
  const hostMatch = /^const LOOPBACK_HTTP_HOSTS := \[([^\]]+)\]$/mu.exec(source);
  assert.ok(hostMatch, "Godot plaintext host allowlist is missing");
  const godotHosts = [...hostMatch[1].matchAll(/"([^"]+)"/gu)].map((match) => match[1]);
  assert.equal(scalarConstant("PRODUCTION_SCHEME"), productionSchemes[0].slice(0, -1));
  assert.equal(scalarConstant("LOOPBACK_HTTP_SCHEME"), mockServer.protocol.slice(0, -1));
  assert.equal(scalarConstant("LOOPBACK_HTTP_PORT"), Number(mockServer.port));
  assert.deepEqual(godotHosts, development.loopback_host_aliases,
    "Godot's plaintext host allowlist must exactly follow OpenAPI");

  assert.match(source, /if authority\.contains\("@"\):[\s\S]*base_url userinfo is forbidden/u);
  assert.match(source, /parsed\.host not in LOOPBACK_HTTP_HOSTS/u,
    "plaintext trust must be exact-host allowlisting, not DNS resolution or suffix matching");
  assert.match(source, /not parsed\.has_port or parsed\.port != LOOPBACK_HTTP_PORT/u);
  assert.match(source, /pending\.request_node\.max_redirects = 0/u,
    "authenticated requests must never automatically follow a redirect to another authority");
  assert.ok(source.indexOf("if not _configuration_error.is_empty()")
      < source.indexOf("var headers_result := _build_headers"),
  "target validation must fail before the Authorization header is constructed");

  for (const executableCounterexample of [
    "remote_plaintext", "wrong_loopback_port", "userinfo_before_loopback",
    "userinfo_before_https", "loopback_suffix", "localhost_suffix", "numeric_loopback",
    "encoded_loopback", "authority_double_port", "redirect_credential_sink",
    "credential-must-not-leak", "Bearer credential followed a redirect",
  ]) {
    assert.ok(runner.includes(executableCounterexample),
      `real Godot runner is missing target-policy counterexample ${executableCounterexample}`);
  }
});

test("Godot validator refuses missing fields and sequence gaps", () => {
  const source = readProjectSource("clients/godot/contract_validator.gd");
  assert.match(source, /CONTRACT_RESPONSE_INVALID/u);
  assert.match(source, /EVENT_SEQUENCE_GAP/u);
  assert.match(source, /expected_after_sequence/u);
  assert.match(source, /contains unknown fields/u);
  assert.match(source, /validate_world_event_page/u);
  assert.match(source, /duplicate event_id/u);
  for (const validator of [
    "validate_skill_build_create_request", "validate_skill_activation", "validate_agent_session_create_request",
    "validate_agent_turn_create_request", "validate_skill_activation_request",
    "validate_client_event_batch_request", "validate_runtime_event",
  ]) {
    assert.match(source, new RegExp(`static func ${validator}\\(`, "u"));
  }
  assert.doesNotMatch(source, /\b(?:int|str|bool)\(/u,
    "the Godot boundary must reject wrong JSON types instead of coercing them");
  assert.doesNotMatch(source, /\.get\([^\n]+,\s*(?:true|"APPLIED"|"SUCCESS")\)/u,
    "validator must not use a successful default for missing fields");
  assert.match(source, /file\.content\.sha256_text\(\) != file\.content_sha256/u,
    "Godot must verify the declared UTF-8 source hash before dispatch");
  assert.match(source, /seen_paths\.has\(file\.path\)/u,
    "Godot must reject duplicate source paths");
  assert.match(source, /entrypoint_matches != 1/u,
    "Godot must bind the entrypoint to exactly one source file");
  const bootstrap = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/examples/game-bootstrap-response.json"), "utf8",
  )).value;
  assert.equal(Number(/const MAX_SOURCE_FILES := ([0-9]+)/u.exec(source)?.[1]),
    bootstrap.limits.max_source_files);
  assert.equal(Number(/const MAX_SOURCE_BYTES := ([0-9]+)/u.exec(source)?.[1]),
    bootstrap.limits.max_source_bytes);
  assert.match(source, /file\.content\.to_utf8_buffer\(\)\.size\(\)/u,
    "Godot source limits must count UTF-8 bytes rather than characters");
});

test("Godot schema projections enforce unique diagnostics, URI references and exact bounds", () => {
  const source = readProjectSource("clients/godot/contract_validator.gd");
  const runner = readProjectSource("clients/godot/contract_test_runner.gd");
  const skillBuildSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/game/skill-build.schema.json"), "utf8",
  ));
  const snapshotSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/game/world-snapshot.schema.json"), "utf8",
  ));
  const bootstrapSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/game/bootstrap-response.schema.json"), "utf8",
  ));
  const commandSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/game/command.schema.json"), "utf8",
  ));
  const sessionSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/game/agent-session.schema.json"), "utf8",
  ));

  assert.equal(skillBuildSchema.properties.phases.items.properties.diagnostic_codes.uniqueItems, true);
  assert.match(source,
    /_validate_unique_string_array\(phase\.diagnostic_codes, 100, 96, "SkillBuild\.phase\.diagnostic_codes"\)/u,
    "Godot must apply the unique diagnostic-code helper at the SkillBuild boundary");
  const uniqueStart = source.indexOf("static func _validate_unique_string_array(");
  const uniqueEnd = source.indexOf("\n\nstatic func ", uniqueStart);
  const uniqueBlock = source.slice(uniqueStart, uniqueEnd);
  assert.match(uniqueBlock, /item in seen/u,
    "the shared array helper must reject duplicates unconditionally");
  assert.doesNotMatch(uniqueBlock, /require_unique/u,
    "callers must not be able to silently disable Schema uniqueItems");
  assert.match(runner, /duplicate_phase_diagnostic/u,
    "the real Godot runner must execute a duplicate diagnostic-code counterexample");

  const worldRulesMaximum = snapshotSchema.properties.world_rules_version.maxLength;
  assert.match(source, new RegExp(
    `_string_with_length\\(value\\.world_rules_version, 1, ${worldRulesMaximum}\\)`, "u",
  ));
  assert.match(runner, new RegExp(`"v"\\.repeat\\(${worldRulesMaximum + 1}\\)`, "u"));

  assert.equal(commandSchema.properties.links.properties.self.format, "uri-reference");
  assert.equal(commandSchema.$defs.resourceCreatedResult.properties.resource_url.format, "uri-reference");
  assert.equal(sessionSchema.properties.links.properties.self.format, "uri-reference");
  for (const property of [
    ...Object.values(commandSchema.properties.links.properties),
    commandSchema.$defs.resourceCreatedResult.properties.resource_url,
    ...Object.values(sessionSchema.properties.links.properties),
    bootstrapSchema.properties.world.properties.snapshot_url,
    bootstrapSchema.properties.world.properties.events_url,
  ]) {
    assert.equal(property.minLength, 1, "public URI references must never be empty");
    assert.equal(property.maxLength, 2048, "public URI references need one shared transport bound");
  }
  assert.match(source, /static func _is_rfc3986_reference\(/u);
  assert.match(source, /not _is_rfc3986_reference\(value\[field\], false\)/u,
    "all Godot link maps must pass through the RFC3986 reference validator");
  assert.match(source, /not _is_rfc3986_reference\(value\.resource_url, false\)/u);
  assert.match(source, /_string_with_length\(value\.resource_url, 1, 2048\)/u);
  assert.match(source, /_string_with_length\(value\[field\], 1, 2048\)/u);
  for (const invalidReference of ["%zz", "[", "a\\\\b", "a|b", "a{b}", "a^b", "://bad", "1http://x"]) {
    assert.ok(runner.includes(`"${invalidReference}"`),
      `the real Godot runner is missing URI counterexample ${invalidReference}`);
  }
});

test("Godot local failures are closed, sanitized and reconciliation-safe", () => {
  const gateway = readProjectSource("clients/godot/agent_api_gateway.gd");
  const runner = readProjectSource("clients/godot/contract_test_runner.gd");
  const validationStart = gateway.indexOf("func _validate_local_transport_error(");
  const validationEnd = gateway.indexOf("\n\nfunc ", validationStart);
  const validationBlock = gateway.slice(validationStart, validationEnd);
  assert.ok(validationStart >= 0 && validationEnd > validationStart,
    "Gateway local transport error validator is missing");
  assert.match(validationBlock,
    /\["scope", "code", "category", "retryable", "operation", "message"\]/u);
  assert.match(validationBlock, /value\.size\(\) != expected_fields\.size\(\)/u,
    "adapter-local errors must reject missing and private fields");
  assert.match(validationBlock, /value\.operation != expected_operation/u,
    "adapter-local errors must bind to the invoked operation");

  const failStart = gateway.indexOf("func _fail_local(");
  const failEnd = gateway.indexOf("\n\nfunc ", failStart);
  const failBlock = gateway.slice(failStart, failEnd);
  assert.match(failBlock, /return _failure_result\(0, \{\}, error\)/u,
    "all locally generated failures must use status 0 and empty headers");
  assert.doesNotMatch(failBlock, /status: int|headers: Dictionary/u,
    "callers must not disguise a local validation failure as a remote HTTP response");
  assert.match(gateway, /_trusted_accepted_reconciliation_context/u);
  assert.match(failBlock, /error\["command_id"\] = context\.command_id/u,
    "trusted command identity must survive local response rejection for reconciliation");

  for (const marker of [
    "valid_local_transport_failure", "private_local_transport_failure", "missing_local_code",
    "wrong_local_operation", "invalid_command_result", "wrong_trace_result",
  ]) {
    assert.match(runner, new RegExp(marker, "u"), `real Godot runner is missing ${marker}`);
  }
  assert.match(runner, /not private_local_result\.error\.has\("debug_secret"\)/u,
    "the executable test must prove adapter-private data is not exposed");
});

test("Godot CommandResult shape and revision cannot drift from JSON Schema", () => {
  const source = readProjectSource("clients/godot/contract_validator.gd");
  const schema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/game/command.schema.json"), "utf8",
  ));
  const start = source.indexOf("static func validate_command_result(");
  const end = source.indexOf("\n\nstatic func ", start);
  assert.ok(start >= 0 && end > start, "Godot CommandResult validator is missing");
  const block = source.slice(start, end);
  const shape = /var shape := _require_shape\(value, \[([\s\S]*?)\], \[\], "CommandResult"\)/u.exec(block);
  assert.ok(shape, "Godot CommandResult strict shape is missing");
  const required = [...shape[1].matchAll(/"([a-z][a-z0-9_]*)"/gu)].map((match) => match[1]);
  assert.deepEqual(required, schema.required,
    "Godot CommandResult strict fields must exactly match the authoritative JSON Schema");
  assert.equal(schema.properties.revision.type, "integer");
  assert.equal(schema.properties.revision.minimum, 1);
  assert.match(block, /if not _is_integer_in_range\(value\.revision, 1\):/u,
    "Godot must reject missing, non-integer, and non-positive command revisions");
});

test("Godot command.stage_changed enforces the authoritative TypeScript status graph", () => {
  const source = readProjectSource("clients/godot/contract_validator.gd");
  const commands = readProjectSource("src/domain/commands.d.ts");
  const asyncApi = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/asyncapi/runtime-events.asyncapi.json"), "utf8",
  ));
  assert.ok(asyncApi.components.schemas.CommandStageChangedPayload["x-invariants"].includes(
    "to_status != from_status",
  ), "AsyncAPI must forbid command.stage_changed self-transitions");
  const graphStart = commands.indexOf("export type CommandStatusSuccessor<");
  const graphEnd = commands.indexOf(";\n\n", graphStart);
  assert.ok(graphStart >= 0 && graphEnd > graphStart, "authoritative CommandStatus graph is missing");
  const authoritativeGraph = Object.fromEntries(
    [...commands.slice(graphStart, graphEnd).matchAll(
      /Status extends "([A-Z][A-Z_]*)"\s*\?\s*([^\r\n]+)/gu,
    )].map((match) => [
      match[1],
      [...match[2].matchAll(/"([A-Z][A-Z_]*)"/gu)]
        .map((target) => target[1])
        .filter((target) => target !== match[1]),
    ]),
  );
  assert.ok(Object.keys(authoritativeGraph).length > 0, "failed to parse CommandStatus graph");

  const constantStart = source.indexOf("const COMMAND_STATUS_SUCCESSORS := {");
  const constantEnd = source.indexOf("\n}", constantStart);
  assert.ok(constantStart >= 0 && constantEnd > constantStart,
    "Godot command status successor map is missing");
  const godotGraph = Object.fromEntries([...source.slice(constantStart, constantEnd).matchAll(
    /^\t"([A-Z][A-Z_]*)": \[([^\]]*)\],$/gmu,
  )].map((match) => [
    match[1],
    [...match[2].matchAll(/"([A-Z][A-Z_]*)"/gu)].map((target) => target[1]),
  ]));
  assert.deepEqual(godotGraph, authoritativeGraph,
    "Godot command status graph must exactly follow CommandStatusSuccessor and reject self-transitions");

  const runtimeStart = source.indexOf("static func _validate_runtime_event_payload(");
  const runtimeMatchStart = source.indexOf("\n\tmatch event_type:", runtimeStart);
  const stageStart = source.indexOf('\t\t"command.stage_changed":', runtimeMatchStart);
  const stageEnd = source.indexOf('\n\t\t"command.terminal":', stageStart);
  const stageBlock = source.slice(stageStart, stageEnd);
  assert.match(stageBlock, /not COMMAND_STATUS_SUCCESSORS\.has\(value\.from_status\)/u);
  assert.match(stageBlock, /value\.to_status not in COMMAND_STATUS_SUCCESSORS\[value\.from_status\]/u);
});

test("Godot runtime revision invariants follow AsyncAPI", () => {
  const source = readProjectSource("clients/godot/contract_validator.gd");
  const asyncApi = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/asyncapi/runtime-events.asyncapi.json"), "utf8",
  ));
  const cases = [
    {
      schema: "SkillActivationAppliedPayload",
      invariant: "registry_revision = previous_registry_revision + 1",
      eventType: "skill.activation.applied",
      expression: /value\.registry_revision != value\.previous_registry_revision \+ 1/u,
    },
    {
      schema: "LearnerModelUpdatedPayload",
      invariant: "learner_revision = previous_revision + 1",
      eventType: "learner.model.updated",
      expression: /value\.learner_revision != value\.previous_revision \+ 1/u,
    },
  ];
  const runtimeStart = source.indexOf("static func _validate_runtime_event_payload(");
  const runtimeMatchStart = source.indexOf("\n\tmatch event_type:", runtimeStart);
  for (const { schema, invariant, eventType, expression } of cases) {
    assert.ok(asyncApi.components.schemas[schema]["x-invariants"].includes(invariant),
      `${schema} must publish its revision invariant`);
    const armStart = source.indexOf(`\t\t"${eventType}":`, runtimeMatchStart);
    const armEnd = source.indexOf("\n\t\t\"", armStart + 3);
    assert.ok(armStart >= 0 && armEnd > armStart, `${eventType} validator branch is missing`);
    assert.match(source.slice(armStart, armEnd), expression,
      `${eventType} must enforce ${invariant}`);
  }
});

test("Godot runtime event payload map cannot drift from AsyncAPI", () => {
  const source = readProjectSource("clients/godot/contract_validator.gd");
  const start = source.indexOf("const RUNTIME_EVENT_PAYLOAD_FIELDS := {");
  const end = source.indexOf("\n}", start);
  assert.ok(start >= 0 && end > start, "Godot runtime event payload map is missing");
  const actual = Object.fromEntries([...source.slice(start, end).matchAll(
    /^\t"([a-z][a-z0-9_.-]+)": \[([^\]]*)\],$/gmu,
  )].map((match) => [
    match[1],
    [...match[2].matchAll(/"([a-z][a-z0-9_]*)"/gu)].map((field) => field[1]),
  ]));
  const asyncApi = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/asyncapi/runtime-events.asyncapi.json"), "utf8",
  ));
  const expected = {};
  for (const message of Object.values(asyncApi.components.messages)) {
    const eventSchemaName = message.payload.$ref.split("/").at(-1);
    const eventSchema = asyncApi.components.schemas[eventSchemaName];
    const envelopeRef = eventSchema.allOf.find((part) => part.$ref)?.$ref;
    if (envelopeRef !== "#/components/schemas/EventEnvelope") continue;
    const specialization = eventSchema.allOf.find((part) => part.properties?.event_type?.const);
    const eventType = specialization.properties.event_type.const;
    const payloadRef = specialization.properties.payload.$ref;
    const payload = asyncApi.components.schemas[payloadRef.split("/").at(-1)];
    expected[eventType] = payload.required;
  }
  assert.deepEqual(actual, expected);
});

test("Godot Run, feedback, action intent and evidence constraints follow JSON Schema", () => {
  const source = readProjectSource("clients/godot/contract_validator.gd");
  const runSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/game/run.schema.json"), "utf8",
  ));
  const feedbackSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/game/agent-turn-feedback.schema.json"), "utf8",
  ));
  const actionSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/game/action-intent.schema.json"), "utf8",
  ));
  const evidenceRefSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/common/evidence-ref.schema.json"), "utf8",
  ));
  const versionSetSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/common/version-set.schema.json"), "utf8",
  ));
  const errorSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/common/error.schema.json"), "utf8",
  ));

  const runStart = source.indexOf("static func validate_run(");
  const runEnd = source.indexOf("\n\nstatic func ", runStart);
  const runBlock = source.slice(runStart, runEnd);
  const runShape = /var shape := _require_shape\(value, \[([\s\S]*?)\], \[\], "Run"\)/u.exec(runBlock);
  assert.ok(runShape, "Godot Run strict shape is missing");
  const runRequired = [...runShape[1].matchAll(/"([a-z][a-z0-9_]*)"/gu)]
    .map((match) => match[1]);
  assert.deepEqual(runRequired, runSchema.required,
    "Godot Run strict fields must exactly match the authoritative JSON Schema");
  assert.match(runBlock, /\(value\.session_id == null\) != \(value\.turn_id == null\)/u,
    "nullable Run owner fields must be both present or both null");
  for (const field of ["session_id", "turn_id", "command_id", "run_id"]) {
    assert.match(runBlock, new RegExp(`value\\.${field}`, "u"),
      `Run feedback owner check must include ${field}`);
  }
  assert.match(runBlock, /_evidence_refs_signature\(value\.agent_feedback\.evidence_refs\)/u);

  const feedbackStart = source.indexOf("static func validate_agent_turn_feedback(");
  const feedbackEnd = source.indexOf("\n\nstatic func ", feedbackStart);
  const feedbackBlock = source.slice(feedbackStart, feedbackEnd);
  const feedbackShape = /var shape := _require_shape\(value, \[([\s\S]*?)\], \[\], "AgentTurnFeedback"\)/u
    .exec(feedbackBlock);
  assert.ok(feedbackShape, "Godot AgentTurnFeedback strict shape is missing");
  const feedbackRequired = [...feedbackShape[1].matchAll(/"([a-z][a-z0-9_]*)"/gu)]
    .map((match) => match[1]);
  assert.deepEqual(feedbackRequired, feedbackSchema.required,
    "Godot AgentTurnFeedback fields must exactly match JSON Schema");

  const actionStart = source.indexOf("static func _validate_action_intent(");
  const actionEnd = source.indexOf("\n\nstatic func ", actionStart);
  const actionBlock = source.slice(actionStart, actionEnd);
  assert.match(
    actionBlock,
    new RegExp(`_is_integer_in_range\\(value\\.amount_ml, 1, ${actionSchema.$defs.water.properties.amount_ml.maximum}\\)`, "u"),
    "Godot WATER maximum must match action-intent.schema.json",
  );
  assert.ok(actionBlock.includes(
    `_validate_pattern(value.interaction, "${actionSchema.$defs.interact.properties.interaction.pattern}"`,
  ), "Godot INTERACT pattern must match action-intent.schema.json");

  const refsStart = source.indexOf("static func _validate_evidence_refs(");
  const refsEnd = source.indexOf("\n\nstatic func ", refsStart);
  const refsBlock = source.slice(refsStart, refsEnd);
  assert.match(refsBlock, /ref\.evidence_id in seen_ids/u,
    "Godot evidence arrays must reject duplicate immutable evidence_id values");
  assert.equal(runSchema.properties.evidence_refs.maxItems, 64);
  assert.ok(runSchema.properties.evidence_refs["x-invariants"].includes(
    "evidence_id values are unique",
  ));
  assert.match(
    source,
    new RegExp(`_string_with_length\\(value\\.uri, ${evidenceRefSchema.properties.uri.minLength}, ${evidenceRefSchema.properties.uri.maxLength}\\)`, "u"),
    "Godot EvidenceRef.uri bounds must match JSON Schema",
  );

  const versionStart = source.indexOf("const VERSION_MAX_LENGTHS := {");
  const versionEnd = source.indexOf("\n}", versionStart);
  const godotVersionLengths = Object.fromEntries([...source.slice(versionStart, versionEnd).matchAll(
    /^\t"([a-z][a-z0-9_]*)": ([0-9]+),$/gmu,
  )].map((match) => [match[1], Number(match[2])]));
  const schemaVersionLengths = Object.fromEntries(Object.entries(versionSetSchema.properties)
    .filter(([, property]) => Number.isInteger(property.maxLength))
    .map(([field, property]) => [field, property.maxLength]));
  assert.deepEqual(godotVersionLengths, schemaVersionLengths,
    "Godot VersionSet maximum lengths must exactly follow version-set.schema.json");
  assert.match(
    source,
    new RegExp(`_string_with_length\\(value\\.message, ${errorSchema.properties.message.minLength}, ${errorSchema.properties.message.maxLength}\\)`, "u"),
    "Godot ContractError.message bounds must match error.schema.json",
  );

  const evidenceSchema = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/schemas/game/evidence.schema.json"), "utf8",
  ));
  const canonicalSpec = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/canonical-json-v1.json"), "utf8",
  ));
  const contractRunner = readProjectSource("clients/godot/contract_test_runner.gd");
  assert.ok(evidenceSchema["x-invariants"].some((invariant) => (
    invariant.includes(canonicalSpec.canonicalization_id)
  )));
  assert.match(source, /canonical_json_sha256_v1\(value\.payload\)/u,
    "Godot must recompute immutable Evidence payload hashes");
  assert.match(source, /_contains_only_unicode_scalars\(value\)/u,
    "Godot canonical JSON must reject ill-formed Unicode strings");
  assert.ok(contractRunner.includes(canonicalSpec.vectors[0].sha256),
    "the real Godot runner must execute the frozen cross-language canonical JSON vector");
  const httpTransportRunner = readProjectSource("clients/godot/http_transport_test_runner.gd");
  assert.ok(httpTransportRunner.includes("\\ud800"),
    "the real Godot HTTP runner must reject an unpaired UTF-16 surrogate before parsing");
});

test("Godot error definitions cannot drift from the shared catalog", () => {
  const source = readProjectSource("clients/godot/contract_validator.gd");
  const start = source.indexOf("const ERROR_DEFINITIONS := {");
  const end = source.indexOf("\n}", start);
  assert.ok(start >= 0 && end > start, "Godot ERROR_DEFINITIONS map is missing");
  const definitions = [...source.slice(start, end).matchAll(
    /^\t"([A-Z][A-Z0-9_]+)": \["([A-Z][A-Z0-9_]+)", (true|false), "([a-z][a-z0-9_.-]+)"\],$/gmu,
  )].map((match) => ({
    code: match[1], category: match[2], retryable: match[3] === "true", user_message_key: match[4],
  })).sort((left, right) => left.code.localeCompare(right.code));
  const catalog = JSON.parse(readFileSync(resolve(PROJECT_ROOT, "contracts/error-catalog.json"), "utf8"));
  const expected = catalog.errors.map(({ code, category, retryable, user_message_key }) => ({
    code, category, retryable, user_message_key,
  })).sort((left, right) => left.code.localeCompare(right.code));
  assert.deepEqual(definitions, expected);

  const statusStart = source.indexOf("const ERROR_HTTP_STATUSES := {");
  const statusEnd = source.indexOf("\n}", statusStart);
  const statuses = Object.fromEntries([...source.slice(statusStart, statusEnd).matchAll(
    /^\t"([A-Z][A-Z0-9_]+)": ([1-5][0-9]{2}),$/gmu,
  )].map((match) => [match[1], Number(match[2])]));
  const expectedStatuses = Object.fromEntries(catalog.errors.map((entry) => [entry.code, entry.http_status]));
  assert.deepEqual(statuses, expectedStatuses);
});
