extends SceneTree

## Fail-closed, read-only release pin for the sibling Agent contract package.
## This gate never invokes Git and therefore permits unrelated dirty files.

const DESCRIPTOR_PATH := "res://contracts/agent-contract-release.json"
const EXPECTED_DESCRIPTOR_VERSION := "1.0.0"
const EXPECTED_PACKAGE_NAME := "@yaya/agent-contracts"
const EXPECTED_PACKAGE_VERSION := "0.6.0"
const EXPECTED_GIT_RELEASE := "refs/tags/agent-contracts-v0.6.0"
const EXPECTED_MANIFEST_SHA256 := "11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05"
const EXPECTED_MANIFEST_BYTES := 27848
const EXPECTED_MANIFEST_FILES := 147
const EXPECTED_STUDENT_BOOTSTRAP_CONTRACT_VERSION := "0.4.0"
const HTTP_METHODS := ["get", "post", "put", "patch", "delete"]
const METHOD_CONSTANTS := {
	"POST": "HTTPClient.METHOD_POST",
	"PUT": "HTTPClient.METHOD_PUT",
	"PATCH": "HTTPClient.METHOD_PATCH",
	"DELETE": "HTTPClient.METHOD_DELETE",
}


func _initialize() -> void:
	var result := _verify_release()
	if not result.ok:
		push_error("CONTRACT_RELEASE_DRIFT_FAIL [%s] %s" % [result.code, result.message])
		quit(1)
		return
	print("CONTRACT_RELEASE_DRIFT_TEST_PASS %s" % JSON.stringify(result.value))
	quit(0)


func _verify_release() -> Dictionary:
	var descriptor_result := _read_json_file(DESCRIPTOR_PATH, "release descriptor")
	if not descriptor_result.ok:
		return descriptor_result
	var descriptor: Dictionary = descriptor_result.value
	var descriptor_guard := _validate_descriptor(descriptor)
	if not descriptor_guard.ok:
		return descriptor_guard
	var root_result := _resolve_agent_root(descriptor.agent_repository)
	if not root_result.ok:
		return root_result
	var agent_root: String = root_result.value

	var manifest_path_result := _repository_path(
		agent_root, str(descriptor.manifest.relative_path), "manifest",
	)
	if not manifest_path_result.ok:
		return manifest_path_result
	var manifest_path: String = manifest_path_result.value
	var manifest_bytes_result := _read_bytes(manifest_path, "Agent contracts manifest")
	if not manifest_bytes_result.ok:
		return manifest_bytes_result
	var manifest_bytes: PackedByteArray = manifest_bytes_result.value
	if manifest_bytes.size() != int(descriptor.manifest.bytes):
		return _failure("MANIFEST_BYTE_LENGTH_DRIFT", "Agent manifest byte length differs from the v0.6 release pin.")
	var manifest_sha := _sha256(manifest_bytes)
	if manifest_sha != str(descriptor.manifest.sha256):
		return _failure("MANIFEST_SHA256_DRIFT", "Agent manifest bytes do not match the pinned SHA-256.")

	var manifest_json_result := _parse_json_bytes(manifest_bytes, "Agent contracts manifest")
	if not manifest_json_result.ok:
		return manifest_json_result
	var manifest: Dictionary = manifest_json_result.value
	var manifest_guard := _validate_manifest(manifest, descriptor)
	if not manifest_guard.ok:
		return manifest_guard
	var file_guard := _verify_manifest_files(agent_root, manifest.files)
	if not file_guard.ok:
		return file_guard
	var sync_guard := _verify_godot_sync(agent_root, descriptor.godot_sync)
	if not sync_guard.ok:
		return sync_guard
	return {
		"ok": true,
		"value": {
			"package_version": descriptor.package_version,
			"git_release": descriptor.git_release,
			"manifest_sha256": manifest_sha,
			"manifest_bytes": manifest_bytes.size(),
			"manifest_files": manifest.files.size(),
			"agent_root": agent_root,
		},
	}


func _validate_descriptor(value: Dictionary) -> Dictionary:
	var root_shape := _closed_shape(value, [
		"descriptor_version", "package_name", "package_version", "git_release",
		"manifest", "agent_repository", "godot_sync",
	], "release descriptor")
	if not root_shape.ok:
		return root_shape
	if (
		value.descriptor_version != EXPECTED_DESCRIPTOR_VERSION
		or value.package_name != EXPECTED_PACKAGE_NAME
		or value.package_version != EXPECTED_PACKAGE_VERSION
		or value.git_release != EXPECTED_GIT_RELEASE
	):
		return _failure("RELEASE_DESCRIPTOR_DRIFT", "The descriptor no longer pins Agent contracts v0.6.0.")
	var manifest_shape := _closed_shape(
		value.manifest, ["relative_path", "sha256", "bytes", "file_count"], "descriptor manifest pin",
	)
	if not manifest_shape.ok:
		return manifest_shape
	if (
		value.manifest.relative_path != "contracts/manifest.json"
		or value.manifest.sha256 != EXPECTED_MANIFEST_SHA256
		or not _exact_integer(value.manifest.bytes, EXPECTED_MANIFEST_BYTES)
		or not _exact_integer(value.manifest.file_count, EXPECTED_MANIFEST_FILES)
	):
		return _failure("RELEASE_MANIFEST_PIN_DRIFT", "The descriptor manifest pin is not the v0.6.0 release candidate.")
	var repository_shape := _closed_shape(
		value.agent_repository,
		["environment_variable", "default_relative_path"],
		"Agent repository locator",
	)
	if not repository_shape.ok:
		return repository_shape
	if (
		value.agent_repository.environment_variable != "YAYA_AGENT_REPOSITORY_ROOT"
		or value.agent_repository.default_relative_path != "../agent"
	):
		return _failure("AGENT_REPOSITORY_LOCATOR_DRIFT", "The Agent repository locator changed unexpectedly.")
	return _validate_sync_descriptor(value.godot_sync)


func _validate_sync_descriptor(value: Variant) -> Dictionary:
	var shape := _closed_shape(value, [
		"api_version", "contract_version", "openapi", "canonical_client",
		"student_operation", "product_operations",
	], "Godot sync descriptor")
	if not shape.ok:
		return shape
	if value.api_version != "1.1.0" or value.contract_version != EXPECTED_STUDENT_BOOTSTRAP_CONTRACT_VERSION:
		return _failure("GODOT_VERSION_PIN_DRIFT", "Godot StudentBootstrapV2 wire versions do not remain on API 1.1.0 / contract 0.4.0.")
	var openapi_shape := _closed_shape(
		value.openapi, ["game", "student_bootstrap", "product"], "OpenAPI locator",
	)
	if not openapi_shape.ok:
		return openapi_shape
	var client_shape := _closed_shape(
		value.canonical_client, ["validator", "gateway", "transport"], "canonical Godot client locator",
	)
	if not client_shape.ok:
		return client_shape
	for name in ["validator", "gateway", "transport"]:
		var entry_shape := _closed_shape(
			value.canonical_client[name],
			["frontend_path", "agent_path", "comparison"],
			"canonical client %s" % name,
		)
		if not entry_shape.ok:
			return entry_shape
	var operation_shape := _validate_operation_descriptor(value.student_operation, "student operation")
	if not operation_shape.ok:
		return operation_shape
	if not value.product_operations is Array or value.product_operations.is_empty():
		return _failure("PRODUCT_OPERATION_DESCRIPTOR_INVALID", "Product operation mappings must be a non-empty Array.")
	var operation_ids := {}
	var client_operations := {}
	for operation in value.product_operations:
		operation_shape = _validate_operation_descriptor(operation, "Product operation")
		if not operation_shape.ok:
			return operation_shape
		if operation_ids.has(operation.operation_id) or client_operations.has(operation.client_operation):
			return _failure("PRODUCT_OPERATION_DESCRIPTOR_DUPLICATE", "Product operation mappings must be unique.")
		operation_ids[operation.operation_id] = true
		client_operations[operation.client_operation] = true
	return {"ok": true}


func _validate_operation_descriptor(value: Variant, label: String) -> Dictionary:
	var shape := _closed_shape(value, [
		"operation_id", "method", "openapi_path", "client_operation", "gateway_method",
	], label)
	if not shape.ok:
		return shape
	for field in ["operation_id", "method", "openapi_path", "client_operation", "gateway_method"]:
		if typeof(value[field]) != TYPE_STRING or value[field].is_empty():
			return _failure("OPERATION_DESCRIPTOR_INVALID", "%s.%s must be a non-empty String." % [label, field])
	if value.method not in ["GET", "POST", "PUT", "PATCH", "DELETE"]:
		return _failure("OPERATION_DESCRIPTOR_INVALID", "%s.method is unsupported." % label)
	return {"ok": true}


func _validate_manifest(manifest: Dictionary, descriptor: Dictionary) -> Dictionary:
	var shape := _closed_shape(manifest, [
		"schema_version", "package_name", "package_version", "git_release", "hash_algorithm", "files",
	], "Agent contracts manifest")
	if not shape.ok:
		return shape
	if (
		manifest.schema_version != "1.0.0"
		or manifest.package_name != descriptor.package_name
		or manifest.package_version != descriptor.package_version
		or manifest.git_release != descriptor.git_release
		or manifest.hash_algorithm != "sha256"
	):
		return _failure("MANIFEST_RELEASE_DRIFT", "Agent manifest release identity differs from the frontend pin.")
	if not manifest.files is Array or manifest.files.size() != int(descriptor.manifest.file_count):
		return _failure("MANIFEST_FILE_COUNT_DRIFT", "Agent manifest file count differs from the frontend pin.")
	return {"ok": true}


func _verify_manifest_files(agent_root: String, files: Array) -> Dictionary:
	var seen := {}
	for entry in files:
		var shape := _closed_shape(entry, ["path", "bytes", "sha256"], "manifest file entry")
		if not shape.ok:
			return shape
		var relative := str(entry.path)
		if seen.has(relative):
			return _failure("MANIFEST_FILE_DUPLICATE", "Agent manifest repeats %s." % relative)
		seen[relative] = true
		var path_result := _repository_path(agent_root, relative, "manifest file")
		if not path_result.ok:
			return path_result
		var bytes_result := _read_bytes(path_result.value, relative)
		if not bytes_result.ok:
			return bytes_result
		var bytes: PackedByteArray = bytes_result.value
		if not _exact_integer(entry.bytes, bytes.size()) or _sha256(bytes) != str(entry.sha256):
			return _failure("CONTRACT_PACKAGE_FILE_DRIFT", "Agent contract file differs from manifest: %s." % relative)
	return {"ok": true}


func _verify_godot_sync(agent_root: String, sync: Dictionary) -> Dictionary:
	var client_result := _verify_canonical_client_sources(agent_root, sync.canonical_client)
	if not client_result.ok:
		return client_result
	var sources: Dictionary = client_result.value
	var version_marker := 'value.api_version != "%s" or value.contract_version != "%s"' % [
		sync.api_version, sync.contract_version,
	]
	if not sources.validator.contains(version_marker):
		return _failure("GODOT_VALIDATOR_VERSION_DRIFT", "StudentBootstrapV2 validator lost its pinned wire versions.")

	var game_result := _read_repository_json(agent_root, sync.openapi.game, "Game OpenAPI")
	if not game_result.ok:
		return game_result
	var student_result := _read_repository_json(agent_root, sync.openapi.student_bootstrap, "StudentBootstrap OpenAPI")
	if not student_result.ok:
		return student_result
	var product_result := _read_repository_json(agent_root, sync.openapi.product, "Product OpenAPI")
	if not product_result.ok:
		return product_result
	var game_operations_result := _openapi_operations(game_result.value, true)
	if not game_operations_result.ok:
		return game_operations_result
	var student_operations_result := _openapi_operations(student_result.value, true)
	if not student_operations_result.ok:
		return student_operations_result
	var product_operations_result := _openapi_operations(product_result.value, false)
	if not product_operations_result.ok:
		return product_operations_result

	var expected_transport_operations: Array[String] = []
	for operation in game_operations_result.value:
		var mapping_guard := _verify_game_mapping(operation, sources.gateway, sources.transport)
		if not mapping_guard.ok:
			return mapping_guard
		expected_transport_operations.append(str(operation.client_operation))
	var student_guard := _verify_declared_mapping(
		sync.student_operation,
		student_operations_result.value,
		sources.gateway,
		sources.transport,
		true,
	)
	if not student_guard.ok:
		return student_guard
	expected_transport_operations.append(str(sync.student_operation.client_operation))

	var declared_product_ids: Array[String] = []
	for mapping in sync.product_operations:
		var mapping_guard := _verify_declared_mapping(
			mapping,
			product_operations_result.value,
			sources.product_gateway,
			sources.transport,
			false,
		)
		if not mapping_guard.ok:
			return mapping_guard
		declared_product_ids.append(str(mapping.operation_id))
		expected_transport_operations.append(str(mapping.client_operation))
	var canonical_product_ids: Array[String] = []
	for operation in product_operations_result.value:
		canonical_product_ids.append(str(operation.operation_id))
	declared_product_ids.sort()
	canonical_product_ids.sort()
	if declared_product_ids != canonical_product_ids:
		return _failure("PRODUCT_OPERATION_SET_DRIFT", "Frontend Product operation descriptors differ from Product OpenAPI.")

	var actual_transport_operations := _transport_operation_arms(sources.transport)
	expected_transport_operations.sort()
	actual_transport_operations.sort()
	if expected_transport_operations != actual_transport_operations:
		return _failure("TRANSPORT_OPERATION_SET_DRIFT", "Frontend HTTP transport operation arms differ from the pinned public OpenAPI set.")
	return {"ok": true}


func _verify_canonical_client_sources(agent_root: String, client: Dictionary) -> Dictionary:
	var sources := {}
	for name in ["validator", "gateway", "transport"]:
		var frontend_result := _read_frontend_source(client[name].frontend_path, "frontend %s" % name)
		if not frontend_result.ok:
			return frontend_result
		var agent_result := _read_repository_source(agent_root, client[name].agent_path, "Agent canonical %s" % name)
		if not agent_result.ok:
			return agent_result
		var frontend: String = frontend_result.value
		var canonical: String = agent_result.value
		match str(client[name].comparison):
			"text_exact_lf":
				pass
			"relocated_preloads_only":
				frontend = frontend.replace(
					"res://addons/yaya_contract_client/contract_validator.gd", "res://contract_validator.gd",
				).replace(
					"res://addons/yaya_contract_client/agent_api_transport.gd", "res://agent_api_transport.gd",
				)
			"canonical_game_plus_product_extensions":
				frontend = frontend.replace(
					"res://addons/yaya_contract_client/agent_api_transport.gd", "res://agent_api_transport.gd",
				).replace(
					"res://addons/yaya_contract_client/strict_json_object_scanner.gd", "res://strict_json_object_scanner.gd",
				)
				var product_start := frontend.find('\t\t"get_product_content_unit":\n')
				var default_arm := frontend.find("\t\t_:\n", product_start)
				if product_start < 0 or default_arm <= product_start:
					return _failure("TRANSPORT_PRODUCT_EXTENSION_DRIFT", "Frontend Product transport extension boundary is missing.")
				frontend = frontend.erase(product_start, default_arm - product_start)
				frontend = frontend.replace(
					"\tvar has_body := method != HTTPClient.METHOD_GET",
					"\tvar has_body := method == HTTPClient.METHOD_POST",
				)
			_:
				return _failure("CANONICAL_CLIENT_COMPARISON_INVALID", "Unknown canonical comparison mode for %s." % name)
		if frontend != canonical:
			return _failure("CANONICAL_GODOT_CLIENT_DRIFT", "Frontend %s differs from the pinned Agent canonical client." % name)
		sources[name] = frontend_result.value
	var product_result := _read_frontend_source(
		"scripts/client/product_interaction_gateway.gd", "frontend Product Gateway",
	)
	if not product_result.ok:
		return product_result
	sources["product_gateway"] = product_result.value
	return {"ok": true, "value": sources}


func _verify_game_mapping(operation: Dictionary, gateway: String, transport: String) -> Dictionary:
	var client_operation := str(operation.client_operation)
	if not gateway.contains("func %s(" % client_operation):
		return _failure("GAME_GATEWAY_OPERATION_DRIFT", "Game Gateway misses %s." % client_operation)
	return _verify_transport_mapping(operation, client_operation, transport)


func _verify_declared_mapping(
	mapping: Dictionary,
	canonical_operations: Array,
	gateway: String,
	transport: String,
	direct_gateway_name: bool,
) -> Dictionary:
	var canonical: Dictionary = {}
	for operation in canonical_operations:
		if operation.operation_id == mapping.operation_id:
			canonical = operation
			break
	if canonical.is_empty():
		return _failure("OPENAPI_OPERATION_MISSING", "Pinned OpenAPI has no %s." % mapping.operation_id)
	if canonical.method != mapping.method or canonical.path != mapping.openapi_path:
		return _failure("OPENAPI_OPERATION_DRIFT", "%s method/path differs from the frontend descriptor." % mapping.operation_id)
	if direct_gateway_name and canonical.client_operation != mapping.client_operation:
		return _failure("OPENAPI_GODOT_OPERATION_DRIFT", "%s x-godot-operation drifted." % mapping.operation_id)
	var function_marker := "func %s(" % str(mapping.gateway_method)
	var start := gateway.find(function_marker)
	if start < 0:
		return _failure("GATEWAY_METHOD_MISSING", "Frontend Gateway misses %s." % mapping.gateway_method)
	var end := gateway.find("\n\n", start)
	var block := gateway.substr(start, gateway.length() - start if end < 0 else end - start)
	if not block.contains('"%s"' % str(mapping.client_operation)):
		return _failure("GATEWAY_OPERATION_DRIFT", "%s does not dispatch %s." % [mapping.gateway_method, mapping.client_operation])
	return _verify_transport_mapping(canonical, str(mapping.client_operation), transport)


func _verify_transport_mapping(operation: Dictionary, client_operation: String, transport: String) -> Dictionary:
	var block_result := _transport_arm(transport, client_operation)
	if not block_result.ok:
		return block_result
	var block: String = block_result.value
	var method := str(operation.method)
	if method == "GET":
		if block.contains("method = HTTPClient.METHOD_"):
			return _failure("TRANSPORT_METHOD_DRIFT", "%s no longer uses the default GET method." % client_operation)
	elif not block.contains("method = %s" % str(METHOD_CONSTANTS.get(method, ""))):
		return _failure("TRANSPORT_METHOD_DRIFT", "%s HTTP method differs from OpenAPI." % client_operation)
	var template_result := _openapi_transport_template(operation.document, operation.path_item, operation.operation, operation.path)
	if not template_result.ok:
		return template_result
	if not block.contains('"%s"' % str(template_result.value)):
		return _failure("TRANSPORT_PATH_DRIFT", "%s HTTP path/query differs from OpenAPI." % client_operation)
	return {"ok": true}


func _openapi_operations(document: Dictionary, require_godot_operation: bool) -> Dictionary:
	if not document.get("paths") is Dictionary:
		return _failure("OPENAPI_INVALID", "OpenAPI paths must be a Dictionary.")
	var operations: Array[Dictionary] = []
	var seen_ids := {}
	for path in document.paths:
		var path_item: Variant = document.paths[path]
		if not path_item is Dictionary:
			return _failure("OPENAPI_INVALID", "OpenAPI path item must be a Dictionary.")
		for method in HTTP_METHODS:
			if not path_item.has(method):
				continue
			var operation: Variant = path_item[method]
			if not operation is Dictionary or typeof(operation.get("operationId")) != TYPE_STRING:
				return _failure("OPENAPI_INVALID", "OpenAPI operation lacks operationId.")
			var operation_id := str(operation.operationId)
			if seen_ids.has(operation_id):
				return _failure("OPENAPI_OPERATION_DUPLICATE", "OpenAPI repeats operationId %s." % operation_id)
			seen_ids[operation_id] = true
			var client_operation := str(operation.get("x-godot-operation", ""))
			if require_godot_operation and client_operation.is_empty():
				return _failure("OPENAPI_GODOT_OPERATION_MISSING", "%s lacks x-godot-operation." % operation_id)
			operations.append({
				"operation_id": operation_id,
				"client_operation": client_operation,
				"method": str(method).to_upper(),
				"path": str(path),
				"document": document,
				"path_item": path_item,
				"operation": operation,
			})
	return {"ok": true, "value": operations}


func _openapi_transport_template(
	document: Dictionary,
	path_item: Dictionary,
	operation: Dictionary,
	path: String,
) -> Dictionary:
	var regex := RegEx.new()
	if regex.compile("\\{[^}]+\\}") != OK:
		return _failure("REGEX_INITIALIZATION_FAILED", "Could not compile the OpenAPI path matcher.")
	var template := regex.sub(path, "%s", true)
	var query: Array[String] = []
	var parameters: Array = []
	if path_item.get("parameters") is Array:
		parameters.append_array(path_item.parameters)
	if operation.get("parameters") is Array:
		parameters.append_array(operation.parameters)
	for raw_parameter in parameters:
		var resolved_result := _resolve_local_reference(document, raw_parameter)
		if not resolved_result.ok:
			return resolved_result
		var parameter: Variant = resolved_result.value
		if parameter is Dictionary and parameter.get("in") == "query":
			query.append("%s=%%s" % str(parameter.get("name", "")))
	if not query.is_empty():
		template += "?" + "&".join(query)
	return {"ok": true, "value": template}


func _resolve_local_reference(document: Dictionary, value: Variant) -> Dictionary:
	if not value is Dictionary or not value.has("$ref"):
		return {"ok": true, "value": value}
	var reference := str(value["$ref"])
	if not reference.begins_with("#/"):
		return _failure("OPENAPI_REFERENCE_UNSUPPORTED", "Only local OpenAPI parameter references are supported.")
	var current: Variant = document
	for raw_segment in reference.substr(2).split("/"):
		var segment := str(raw_segment).replace("~1", "/").replace("~0", "~")
		if not current is Dictionary or not current.has(segment):
			return _failure("OPENAPI_REFERENCE_INVALID", "OpenAPI reference cannot be resolved: %s." % reference)
		current = current[segment]
	return {"ok": true, "value": current}


func _transport_arm(source: String, operation: String) -> Dictionary:
	var marker := '\t\t"%s":' % operation
	var start := source.find(marker)
	if start < 0:
		return _failure("TRANSPORT_OPERATION_MISSING", "HTTP transport misses %s." % operation)
	var next_arm := source.find("\n\t\t\"", start + marker.length())
	var default_arm := source.find("\n\t\t_:", start + marker.length())
	var end := source.length()
	if next_arm >= 0:
		end = next_arm
	if default_arm >= 0 and default_arm < end:
		end = default_arm
	return {"ok": true, "value": source.substr(start, end - start)}


func _transport_operation_arms(source: String) -> Array[String]:
	var function_start := source.find("func _build_request_spec(")
	var function_end := source.find("\n\nfunc ", function_start)
	if function_start < 0 or function_end <= function_start:
		return []
	var function_source := source.substr(function_start, function_end - function_start)
	var regex := RegEx.new()
	if regex.compile('(?m)^\\t\\t"([a-z][a-z0-9_]{2,63})":$') != OK:
		return []
	var operations: Array[String] = []
	for regex_match in regex.search_all(function_source):
		operations.append(regex_match.get_string(1))
	return operations


func _resolve_agent_root(locator: Dictionary) -> Dictionary:
	var frontend_root := ProjectSettings.globalize_path("res://").simplify_path()
	var configured := OS.get_environment(str(locator.environment_variable)).strip_edges()
	var candidate := configured if not configured.is_empty() else str(locator.default_relative_path)
	if not candidate.is_absolute_path():
		candidate = frontend_root.path_join(candidate)
	candidate = candidate.simplify_path()
	if not DirAccess.dir_exists_absolute(candidate):
		return _failure("AGENT_REPOSITORY_MISSING", "Agent repository is unavailable; set %s or provide sibling ../agent." % locator.environment_variable)
	return {"ok": true, "value": candidate}


func _repository_path(root: String, relative: String, label: String) -> Dictionary:
	if (
		relative.is_empty()
		or relative.is_absolute_path()
		or relative.contains("\\")
		or relative.contains(":")
		or ".." in relative.split("/")
	):
		return _failure("REPOSITORY_PATH_INVALID", "%s path is not a safe repository-relative path." % label)
	return {"ok": true, "value": root.path_join(relative).simplify_path()}


func _read_repository_json(root: String, relative: String, label: String) -> Dictionary:
	var path_result := _repository_path(root, relative, label)
	if not path_result.ok:
		return path_result
	return _read_json_file(path_result.value, label)


func _read_repository_source(root: String, relative: String, label: String) -> Dictionary:
	var path_result := _repository_path(root, relative, label)
	if not path_result.ok:
		return path_result
	return _read_source(path_result.value, label)


func _read_frontend_source(relative: String, label: String) -> Dictionary:
	if relative.is_empty() or relative.is_absolute_path() or ".." in relative.split("/"):
		return _failure("FRONTEND_PATH_INVALID", "%s path is invalid." % label)
	return _read_source("res://" + relative, label)


func _read_source(path: String, label: String) -> Dictionary:
	if not FileAccess.file_exists(path):
		return _failure("SOURCE_FILE_MISSING", "%s is missing." % label)
	var source := FileAccess.get_file_as_string(path).replace("\r\n", "\n")
	if source.is_empty():
		return _failure("SOURCE_FILE_EMPTY", "%s is empty or unreadable." % label)
	return {"ok": true, "value": source}


func _read_json_file(path: String, label: String) -> Dictionary:
	var bytes_result := _read_bytes(path, label)
	if not bytes_result.ok:
		return bytes_result
	return _parse_json_bytes(bytes_result.value, label)


func _read_bytes(path: String, label: String) -> Dictionary:
	if not FileAccess.file_exists(path):
		return _failure("RELEASE_FILE_MISSING", "%s is missing." % label)
	var bytes := FileAccess.get_file_as_bytes(path)
	if bytes.is_empty():
		return _failure("RELEASE_FILE_EMPTY", "%s is empty or unreadable." % label)
	return {"ok": true, "value": bytes}


func _parse_json_bytes(bytes: PackedByteArray, label: String) -> Dictionary:
	var text := bytes.get_string_from_utf8()
	if text.to_utf8_buffer() != bytes:
		return _failure("RELEASE_JSON_UTF8_INVALID", "%s is not canonical UTF-8." % label)
	var json := JSON.new()
	if json.parse(text) != OK or not json.data is Dictionary:
		return _failure("RELEASE_JSON_INVALID", "%s is not a JSON object." % label)
	return {"ok": true, "value": json.data}


func _sha256(bytes: PackedByteArray) -> String:
	var context := HashingContext.new()
	if context.start(HashingContext.HASH_SHA256) != OK:
		return ""
	if context.update(bytes) != OK:
		return ""
	return context.finish().hex_encode()


func _closed_shape(value: Variant, required: Array, label: String) -> Dictionary:
	if not value is Dictionary or value.size() != required.size():
		return _failure("RELEASE_SHAPE_INVALID", "%s has missing or unknown fields." % label)
	for field in required:
		if not value.has(field):
			return _failure("RELEASE_SHAPE_INVALID", "%s is missing %s." % [label, field])
	return {"ok": true}


func _exact_integer(value: Variant, expected: int) -> bool:
	return (
		(typeof(value) == TYPE_INT and value == expected)
		or (typeof(value) == TYPE_FLOAT and value == float(expected))
	)


func _failure(code: String, message: String) -> Dictionary:
	return {"ok": false, "code": code, "message": message}
