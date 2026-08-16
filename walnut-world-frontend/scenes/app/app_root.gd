extends Control

## Runtime composition root for the Godot student client. The scene accepts
## only the public Gateway base URL; the bearer token is runtime-only and the
## Session/Build/Activation/World authority comes from StudentBootstrap.

const HttpTransport = preload("res://scripts/client/audited_http_agent_api_transport.gd")
const GameGateway = preload("res://addons/yaya_contract_client/agent_api_gateway.gd")
const ProductGateway = preload("res://scripts/client/product_interaction_gateway.gd")
const ProductCapabilityGateway = preload("res://scripts/client/product_capability_gateway.gd")
const WorldPresentationGateway = preload("res://scripts/client/world_presentation_gateway.gd")
const CommandPollerScript = preload("res://scripts/client/command_poller.gd")

signal startup_finished(result: Dictionary)

@export var api_base_url: String = ""
@export var world_presentation_enabled := false
@export var skill_patch_enabled := false

@onready var store: WalnutClientStore = get_node_or_null("/root/ClientStore") as WalnutClientStore
@onready var session_controller: Node = get_node_or_null("/root/SessionController")
@onready var world_event_player: Node = get_node_or_null("WorldEventPlayer")
@onready var world_presentation_renderer: Node = get_node_or_null("TaskWorkspace/WorldViewport")
@onready var task_workspace: Control = get_node_or_null("TaskWorkspace") as Control

## Non-exported test seam. A headless test may inject loopback endpoint/token
## state before adding the real app_root.tscn to the tree; no secret is saved in
## the scene and the production HTTP transport/Gateways remain in use.
var runtime_environment_override: Dictionary = {}
var poller_settings_override: Dictionary = {}

var _transport: RefCounted
var _game_gateway: RefCounted
var _product_gateway: RefCounted
var _product_capability_gateway: RefCounted
var _world_presentation_gateway: RefCounted
var _bootstrap: Dictionary = {}
var _starting := false
var _startup_reported := false


func _enter_tree() -> void:
	var scene_tree := Engine.get_main_loop() as SceneTree
	var client_store := (
		scene_tree.root.get_node_or_null("ClientStore") as WalnutClientStore
		if scene_tree != null
		else null
	)
	if client_store != null and client_store.has_method("begin_authority_revalidation"):
		client_store.begin_authority_revalidation()
		client_store.set_flow(WalnutClientStore.FlowState.BOOTSTRAPPING)
	var controller := (
		scene_tree.root.get_node_or_null("SessionController")
		if scene_tree != null
		else null
	)
	if controller != null and controller.has_method("begin_startup_authority_revalidation"):
		controller.begin_startup_authority_revalidation()


func _ready() -> void:
	call_deferred("_start")


func _exit_tree() -> void:
	if _transport != null and _transport.has_method("shutdown"):
		_transport.shutdown()


func _start() -> void:
	if _starting:
		return
	_starting = true
	if store == null or session_controller == null:
		_fail("APP_AUTOLOAD_UNAVAILABLE", "ClientStore or SessionController autoload is unavailable.")
		return
	if store.has_method("persistence_integrity_result"):
		var persistence_integrity: Dictionary = store.persistence_integrity_result()
		if not bool(persistence_integrity.get("ok", false)):
			_fail_from_result(
				"CLIENT_PERSISTENCE_CORRUPT",
				"Persisted client authority is corrupt and cannot be replaced implicitly.",
				persistence_integrity,
			)
			return
	var configuration: Dictionary = resolve_configuration(api_base_url, _runtime_environment())
	if not bool(configuration.get("ok", false)):
		_fail(
			str(configuration.get("error_code", "APP_CONFIGURATION_INVALID")),
			str(configuration.get("message", "Application configuration is invalid.")),
		)
		return

	_transport = HttpTransport.new(
		self,
		str(configuration.base_url),
		str(configuration.bearer_token),
	)
	_game_gateway = GameGateway.new(_transport)
	_product_gateway = ProductGateway.new(_transport)
	_product_capability_gateway = ProductCapabilityGateway.new(_transport)
	_world_presentation_gateway = WorldPresentationGateway.new(_transport)
	session_controller.configure(_game_gateway, _product_gateway)
	if not session_controller.has_method("configure_world_presentation"):
		_fail("WORLD_PRESENTATION_COMPOSITION_UNAVAILABLE", "SessionController cannot assemble authoritative World playback.")
		return
	session_controller.configure_world_presentation(
		_world_presentation_gateway,
		world_event_player,
		world_presentation_renderer,
		world_presentation_enabled,
	)
	if session_controller.has_method("configure_polling"):
		session_controller.configure_polling(poller_settings_override)
	store.set_flow(WalnutClientStore.FlowState.BOOTSTRAPPING)

	var bootstrap_result: Dictionary = await _game_gateway.get_student_bootstrap(
		RequestContextFactory.new_wire_attempt(),
	)
	if not is_instance_valid(self):
		return
	if not bool(bootstrap_result.get("ok", false)):
		_fail_from_result(
			"STUDENT_BOOTSTRAP_FAILED",
			"Student bootstrap authority could not be recovered.",
			bootstrap_result,
		)
		return
	_bootstrap = bootstrap_result.value.duplicate(true)
	var capability_guard := _validate_required_capabilities(_bootstrap)
	if not capability_guard.ok:
		_fail(str(capability_guard.error_code), str(capability_guard.message))
		return
	var binding_result: Dictionary = store.bind_authority(
		str(configuration.base_url),
		_bootstrap,
	)
	if not binding_result.get("ok", false):
		_fail_from_result(
			"CLIENT_AUTHORITY_BINDING_INVALID",
			"The new Bootstrap could not be bound to this API origin and actor/content identity.",
			binding_result,
		)
		return
	store.set_authoritative_bootstrap(_bootstrap)
	if session_controller.has_method("configure_authority"):
		session_controller.configure_authority(_bootstrap, {})
	var skill_patch_configuration: Dictionary = await _configure_skill_patch_capability()
	if not is_instance_valid(self):
		return
	if not skill_patch_configuration.get("ok", false):
		_fail_from_result(
			"SKILL_PATCH_CAPABILITY_RECOVERY_FAILED",
			"Skill Patch rollout authority could not be recovered safely.",
			skill_patch_configuration,
		)
		return

	var content_result: Dictionary = await _product_gateway.get_content(
		_new_request_context(),
		_bootstrap.content,
	)
	if not is_instance_valid(self):
		return
	if not bool(content_result.get("ok", false)):
		_fail_from_result(
			"CONTENT_RECOVERY_FAILED",
			"Version-pinned task content could not be recovered.",
			content_result,
		)
		return
	store.set_content(content_result.value)

	var session_result := await _recover_or_create_session()
	if not is_instance_valid(self):
		return
	if not session_result.get("ok", false):
		_fail_from_result(
			"SESSION_RECOVERY_FAILED",
			"The authoritative AgentSession could not be recovered.",
			session_result,
		)
		return
	var session: Dictionary = session_result.value
	if not _session_matches_bootstrap(session):
		_fail(
			"SESSION_AUTHORITY_MISMATCH",
			"The exact AgentSession disagrees with StudentBootstrap authority.",
		)
		return
	store.set_authoritative_session(session)
	if session_controller.has_method("configure_authority"):
		session_controller.configure_authority(_bootstrap, session)
	if store.has_method("complete_authority_revalidation"):
		var revalidation_result: Dictionary = store.complete_authority_revalidation(_bootstrap, session)
		if not bool(revalidation_result.get("ok", false)):
			_fail_from_result(
				"CLIENT_AUTHORITY_REVALIDATION_FAILED",
				"Persisted World authority could not be revalidated against the exact Bootstrap and Session.",
				revalidation_result,
			)
			return

	var recovery: Dictionary = await session_controller.recover_workspace(
		_new_request_context(),
		str(session.session_id),
	)
	if not is_instance_valid(self):
		return
	if not bool(recovery.get("ok", false)):
		_fail_from_result(
			"WORKSPACE_RECOVERY_FAILED",
			"Task workspace could not be restored from canonical resources.",
			recovery,
		)
		return
	if world_presentation_enabled:
		if not session_controller.has_method("synchronize_world_presentation_cursor"):
			_fail("WORLD_PRESENTATION_SYNCHRONIZATION_UNAVAILABLE", "SessionController cannot synchronize the presentation high watermark.")
			return
		var presentation_sync: Dictionary = await session_controller.synchronize_world_presentation_cursor()
		if not is_instance_valid(self):
			return
		if not bool(presentation_sync.get("ok", false)):
			_fail_from_result(
				"WORLD_PRESENTATION_SYNCHRONIZATION_FAILED",
				"Authoritative World presentation high watermark could not be synchronized.",
				presentation_sync,
			)
			return
	# A persisted Draft save owns its original CAS base, timestamp and
	# Idempotency-Key.  Reconcile it before READY so a response-loss restart
	# cannot silently discard the student's edit or construct a changed PUT.
	if not session_controller.has_method("recover_pending_draft_save_operations"):
		_fail("PENDING_DRAFT_RECOVERY_UNAVAILABLE", "SessionController cannot recover persisted Draft save envelopes.")
		return
	var pending_draft_recovery: Dictionary = await session_controller.recover_pending_draft_save_operations()
	if not is_instance_valid(self):
		return
	if not bool(pending_draft_recovery.get("ok", false)):
		_fail_from_result(
			"PENDING_DRAFT_RECOVERY_FAILED",
			"A persisted Draft save could not be reconciled without changing its identity.",
			pending_draft_recovery,
		)
		return
	var pending_draft_value: Dictionary = pending_draft_recovery.get("value", {})
	var pending_draft_terminal_error: Variant = pending_draft_value.get("terminal_error")
	if pending_draft_terminal_error is Dictionary and not pending_draft_terminal_error.is_empty():
		_fail(
			str(pending_draft_terminal_error.get("code", "PENDING_DRAFT_TERMINAL_FAILURE")),
			str(pending_draft_terminal_error.get("message", "The persisted Draft save reached a terminal failure.")),
		)
		return
	if not session_controller.has_method("recover_pending_patch_decisions"):
		_fail("PENDING_PATCH_RECOVERY_UNAVAILABLE", "SessionController cannot recover persisted PatchDecision envelopes.")
		return
	var pending_patch_recovery: Dictionary = await session_controller.recover_pending_patch_decisions()
	if not is_instance_valid(self):
		return
	if not bool(pending_patch_recovery.get("ok", false)):
		_fail_from_result(
			"PENDING_PATCH_RECOVERY_FAILED",
			"A persisted PatchDecision could not be reconciled from its exact request bytes.",
			pending_patch_recovery,
		)
		return
	if not session_controller.has_method("recover_pending_patch_request"):
		_fail("PENDING_PATCH_REQUEST_RECOVERY_UNAVAILABLE", "SessionController cannot recover persisted explicit Skill Patch requests.")
		return
	var pending_patch_request: Dictionary = await session_controller.recover_pending_patch_request()
	if not is_instance_valid(self):
		return
	if not bool(pending_patch_request.get("ok", false)):
		_fail_from_result(
			"PENDING_PATCH_REQUEST_RECOVERY_FAILED",
			"A persisted explicit Skill Patch request could not be reconciled without Run/World mutation.",
			pending_patch_request,
		)
		return
	# Workspace may already expose the high-water marks produced by a Turn
	# whose HTTP response was lost. Reconcile the persisted envelope before
	# READY so startup cannot derive a second Turn identity from those marks.
	if not session_controller.has_method("recover_pending_turn_operations"):
		_fail("PENDING_TURN_RECOVERY_UNAVAILABLE", "SessionController cannot recover persisted Agent Turn envelopes.")
		return
	var pending_recovery: Dictionary = await session_controller.recover_pending_turn_operations(true)
	if not is_instance_valid(self):
		return
	if not bool(pending_recovery.get("ok", false)):
		_fail_from_result(
			"PENDING_TURN_RECOVERY_FAILED",
			"A persisted Agent Turn could not be reconciled without changing its identity.",
			pending_recovery,
		)
		return
	var pending_value: Dictionary = pending_recovery.get("value", {})
	var terminal_error: Variant = pending_value.get("terminal_error")
	if terminal_error is Dictionary and not terminal_error.is_empty():
		_fail(
			str(terminal_error.get("code", "PENDING_TURN_TERMINAL_FAILURE")),
			str(terminal_error.get("message", "The persisted Agent Turn reached a terminal failure.")),
		)
		return
	if bool(pending_value.get("had_pending", false)):
		if session_controller.has_method("set_startup_authority_ready"):
			session_controller.set_startup_authority_ready(true)
		_finish({
			"ok": true,
			"session_id": str(session.session_id),
			"pending_turn_recovered": true,
			"pending_turn_outcomes": pending_value.get("outcomes", []).duplicate(true),
		})
		return
	store.set_flow(WalnutClientStore.FlowState.READY)
	if session_controller.has_method("set_startup_authority_ready"):
		session_controller.set_startup_authority_ready(true)
	var patch_failure_recovery_status: Dictionary = {}
	if session_controller.has_method("patch_failure_recovery_result"):
		patch_failure_recovery_status = session_controller.patch_failure_recovery_result()
	var patch_failure_recovered := (
		session_controller.has_method("can_request_ai_patch")
		and bool(session_controller.can_request_ai_patch())
		and not bool(store.objective_result.get("objective_succeeded", true))
	)
	if patch_failure_recovery_status.is_empty() and not patch_failure_recovered:
		store.set_objective_result({
			"summary": "Session, Draft, active Skill tuple, World Snapshot and interactions were restored from public authority.",
		})
	elif not patch_failure_recovery_status.is_empty():
		var patch_error: Dictionary = patch_failure_recovery_status.get("error", {})
		store.set_objective_result({
			"summary": "[%s] %s" % [
				str(patch_error.get("code", "SKILL_PATCH_RECOVERY_NOT_PROVEN")),
				str(patch_error.get("message", "Recovered Skill Patch eligibility is not proven.")),
			],
			"skill_patch_recovery": patch_failure_recovery_status.duplicate(true),
		})
	_finish({"ok": true, "session_id": str(session.session_id)})


func _configure_skill_patch_capability() -> Dictionary:
	if not session_controller.has_method("configure_skill_patch_capability"):
		return _local_failure("SKILL_PATCH_COMPOSITION_UNAVAILABLE", "SessionController has no Skill Patch capability gate.")
	if task_workspace == null or not task_workspace.has_method("configure_skill_patch_enabled"):
		return _local_failure("SKILL_PATCH_UI_UNAVAILABLE", "Formal TaskWorkspace has no Skill Patch capability gate.")
	if not skill_patch_enabled or not world_presentation_enabled:
		session_controller.configure_skill_patch_capability({})
		task_workspace.configure_skill_patch_enabled(false)
		return {"ok": true, "status": 200, "headers": {}, "value": {"enabled": false}}
	if _product_capability_gateway == null:
		return _local_failure("SKILL_PATCH_CAPABILITY_GATEWAY_UNAVAILABLE", "Product capability Gateway is unavailable.")
	var result: Dictionary = await _product_capability_gateway.get_product_capabilities(
		RequestContextFactory.new_wire_attempt(),
		_bootstrap.actor,
		_bootstrap.content,
	)
	if not result.get("ok", false):
		return result
	var capability: Dictionary = result.value
	var enabled := (
		bool(capability.get("world_presentation_enabled", false))
		and bool(capability.get("skill_patch_enabled", false))
	)
	session_controller.configure_skill_patch_capability(capability if enabled else {})
	task_workspace.configure_skill_patch_enabled(enabled)
	return {
		"ok": true, "status": 200, "headers": result.get("headers", {}).duplicate(true),
		"value": {"enabled": enabled, "capability": capability.duplicate(true)},
	}


static func resolve_configuration(fallback_base_url: String, environment: Dictionary) -> Dictionary:
	var environment_base_url := str(environment.get("YAYA_API_BASE_URL", "")).strip_edges()
	var base_url := (
		environment_base_url
		if not environment_base_url.is_empty()
		else fallback_base_url.strip_edges()
	)
	var bearer_token := str(environment.get("YAYA_AUTH_TOKEN", "")).strip_edges()
	if base_url.is_empty():
		return _configuration_failure(
			"API_BASE_URL_MISSING",
			"Set YAYA_API_BASE_URL or the AppRoot api_base_url export.",
		)
	if bearer_token.is_empty():
		return _configuration_failure(
			"AUTH_TOKEN_MISSING",
			"Set YAYA_AUTH_TOKEN in the runtime environment; tokens are never stored in scenes.",
		)
	var normalized_base_url := WalnutClientStore.normalize_api_base_url(base_url)
	if normalized_base_url.is_empty():
		return _configuration_failure(
			"API_BASE_URL_INVALID",
			"The API base URL cannot be normalized into an origin namespace.",
		)
	return {"ok": true, "base_url": normalized_base_url, "bearer_token": bearer_token}


static func _validate_required_capabilities(bootstrap: Dictionary) -> Dictionary:
	var capabilities: Variant = bootstrap.get("capabilities")
	if not capabilities is Dictionary:
		return _configuration_failure(
			"STUDENT_CAPABILITIES_INVALID",
			"StudentBootstrap capabilities authority is absent.",
		)
	for capability in ["skill_builds", "skill_activations", "agent_sessions", "http_world_recovery", "evidence_query"]:
		if not bool(capabilities.get(capability, false)):
			return _configuration_failure(
				"STUDENT_CAPABILITY_UNAVAILABLE",
				"StudentBootstrap does not grant required %s capability." % capability,
			)
	return {"ok": true}


func _runtime_environment() -> Dictionary:
	if not runtime_environment_override.is_empty():
		return runtime_environment_override.duplicate(true)
	return {
		"YAYA_API_BASE_URL": OS.get_environment("YAYA_API_BASE_URL"),
		"YAYA_AUTH_TOKEN": OS.get_environment("YAYA_AUTH_TOKEN"),
	}


func _new_request_context() -> Dictionary:
	if _bootstrap.is_empty():
		return {}
	return RequestContextFactory.new_attempt(_bootstrap.actor, _bootstrap.content)


func _recover_or_create_session() -> Dictionary:
	var session_authority: Variant = _bootstrap.get("session")
	if not session_authority is Dictionary:
		return _local_failure("SESSION_AUTHORITY_INVALID", "StudentBootstrap session authority is absent.")
	var current_session_id: Variant = session_authority.get("current_session_id")
	if current_session_id != null:
		if typeof(current_session_id) != TYPE_STRING or current_session_id.is_empty():
			return _local_failure("SESSION_AUTHORITY_INVALID", "current_session_id is invalid.")
		var pending_integrity: Dictionary = store.validate_pending_operation("agent_session_create")
		if not pending_integrity.get("ok", false):
			return pending_integrity
		var pending: Dictionary = pending_integrity.get("value", {})
		if not pending.is_empty() and not _pending_session_create_matches_bootstrap(pending, session_authority):
			return _local_failure(
				"SESSION_CREATE_RECOVERY_AUTHORITY_MISMATCH",
				"Persisted Session creation does not match the current Bootstrap authority.",
			)
		var recovered: Dictionary = await _game_gateway.get_agent_session(
			_new_request_context(),
			current_session_id,
		)
		if not recovered.get("ok", false) or pending.is_empty():
			return recovered
		if not recovered.get("value") is Dictionary or not _session_matches_bootstrap(recovered.value):
			return _local_failure(
				"SESSION_CREATE_RECOVERY_RESOURCE_MISMATCH",
				"Bootstrap current_session_id did not resolve the persisted Session creation authority.",
			)
		if not store.clear_pending_operation("agent_session_create"):
			return _local_failure(
				"SESSION_CREATE_RECOVERY_CLEAR_FAILED",
				"The reconciled Session creation envelope could not be durably cleared.",
			)
		return recovered

	var create_request: Variant = session_authority.get("create_request")
	if not create_request is Dictionary:
		return _local_failure("SESSION_CREATE_AUTHORITY_INVALID", "Session create_request is absent.")
	var identity := _session_create_identity(create_request)
	var proposed := {
		"idempotency_key": RequestContextFactory.idempotency_key_for(
			"createAgentSession",
			identity,
		),
		"request": create_request.duplicate(true),
	}
	var envelope_result := store.ensure_pending_operation("agent_session_create", identity, proposed)
	if not envelope_result.get("ok", false):
		return envelope_result
	var envelope: Dictionary = envelope_result.value
	var submission: Dictionary = await _game_gateway.create_agent_session(
		_new_request_context(),
		str(envelope.idempotency_key),
		envelope.request,
	)
	var poller := CommandPollerScript.new(
		_game_gateway,
		Callable(self, "_new_request_context"),
		poller_settings_override,
	)
	var command_result: Dictionary = await poller.reconcile({}, submission)
	if not command_result.get("ok", false):
		return command_result
	var command: Dictionary = command_result.value
	if str(command.get("status", "")) != "APPLIED":
		store.clear_pending_operation("agent_session_create")
		return _local_failure("SESSION_CREATE_COMMAND_REJECTED", "Session creation did not reach APPLIED.")
	var resource: Variant = command.get("result")
	if (
		not resource is Dictionary
		or str(resource.get("result_type", "")) != "RESOURCE_CREATED"
		or str(resource.get("resource_type", "")) != "AGENT_SESSION"
		or str(resource.get("resource_id", "")).is_empty()
	):
		return _local_failure(
			"SESSION_CREATE_COMMAND_INVALID",
			"Session command did not name one exact AgentSession resource.",
		)
	var result: Dictionary = await _game_gateway.get_agent_session(
		_new_request_context(),
		str(resource.resource_id),
	)
	if result.get("ok", false):
		if not store.clear_pending_operation("agent_session_create"):
			return _local_failure(
				"SESSION_CREATE_RECOVERY_CLEAR_FAILED",
				"The reconciled Session creation envelope could not be durably cleared.",
			)
	return result


func _pending_session_create_matches_bootstrap(
	envelope: Dictionary,
	session_authority: Dictionary,
) -> bool:
	var request: Variant = envelope.get("request")
	var create_request: Variant = session_authority.get("create_request")
	if not request is Dictionary or not create_request is Dictionary or request != create_request:
		return false
	var identity := _session_create_identity(create_request)
	return (
		not identity.is_empty()
		and str(envelope.get("idempotency_key", ""))
			== RequestContextFactory.idempotency_key_for("createAgentSession", identity)
	)


static func _session_create_identity(request: Dictionary) -> String:
	var content: Variant = request.get("content")
	if not content is Dictionary:
		return ""
	# JSON object member order is not semantic and may change across bootstrap
	# responses. Hash the frozen request fields in one explicit array while the
	# request body itself remains the exact authoritative Dictionary.
	return JSON.stringify([
		request.get("world_id"),
		request.get("learner_id"),
		request.get("agent_profile_id"),
		request.get("channel"),
		request.get("locale"),
		content.get("unit_id"),
		content.get("version"),
		content.get("content_hash"),
		request.get("expected_world_revision"),
	]).sha256_text()


func _session_matches_bootstrap(session: Dictionary) -> bool:
	var session_authority: Variant = _bootstrap.get("session")
	if not session_authority is Dictionary:
		return false
	var create_request: Variant = session_authority.get("create_request")
	if not create_request is Dictionary:
		return false
	var origin: Variant = session.get("request_context")
	var versions: Variant = session.get("versions")
	return (
		str(session.get("status", "")) == "ACTIVE"
		and origin is Dictionary
		and origin.get("actor") == _bootstrap.actor
		and origin.get("content_ref") == _bootstrap.content
		and str(session.get("world_id", "")) == str(_bootstrap.world.world_id)
		and str(session.get("learner_id", "")) == str(create_request.learner_id)
		and str(session.get("agent_profile_id", "")) == str(create_request.agent_profile_id)
		and str(session.get("channel", "")) == str(create_request.channel)
		and session.get("content") == _bootstrap.content
		and versions is Dictionary
		and versions.get("teaching_spec_version") == session_authority.get("teaching_spec_version")
	)


func _fail_from_result(default_code: String, default_message: String, result: Dictionary) -> void:
	var error: Variant = result.get("error")
	if error is Dictionary:
		var details: Variant = error.get("error")
		_fail(
			str(error.get("code", details.get("code", default_code) if details is Dictionary else default_code)),
			str(error.get("message", details.get("message", default_message) if details is Dictionary else default_message)),
		)
		return
	_fail(default_code, default_message)


func _fail(code: String, message: String) -> void:
	if store != null:
		store.report_error({"scope": "CLIENT_LOCAL", "code": code, "message": message})
	_finish({"ok": false, "error_code": code, "message": message})


func _finish(result: Dictionary) -> void:
	if _startup_reported:
		return
	_startup_reported = true
	startup_finished.emit(result.duplicate(true))


static func _configuration_failure(error_code: String, message: String) -> Dictionary:
	return {"ok": false, "error_code": error_code, "message": message}


static func _local_failure(code: String, message: String) -> Dictionary:
	return {
		"ok": false,
		"status": 0,
		"headers": {},
		"error": {
			"scope": "CLIENT_LOCAL",
			"code": code,
			"message": message,
			"retryable": false,
		},
	}
