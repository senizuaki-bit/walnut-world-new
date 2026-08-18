extends SceneTree

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const ProductGatewayScript := preload("res://scripts/client/product_interaction_gateway.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")
const StoreScript := preload("res://autoload/client_store.gd")
const AgentContractFixtureLocator := preload("res://scripts/testing/agent_contract_fixture_locator.gd")

const PERSISTENCE_PATH := "user://skill_patch_visible_failure_restart_test.json"


class Game:
	extends RefCounted
	var build: Dictionary
	var run: Dictionary
	var evidence: Dictionary
	var build_reads := 0
	var run_reads := 0
	var evidence_reads := 0
	var mutation_attempts := 0
	var missing_build := false
	var drift_run := false
	var drift_evidence := false

	func get_skill_build(_attempt: Dictionary, build_id: String) -> Dictionary:
		build_reads += 1
		if missing_build:
			return {"ok": false, "status": 404, "headers": {}, "error": {"code": "NOT_FOUND", "message": "Build disappeared."}}
		return {"ok": true, "status": 200, "headers": {}, "value": build.duplicate(true)} if build_id == str(build.build_id) else {"ok": false, "status": 404, "headers": {}, "error": {"code": "NOT_FOUND"}}

	func get_run(_attempt: Dictionary, run_id: String) -> Dictionary:
		run_reads += 1
		var value := run.duplicate(true)
		if drift_run:
			value.agent_feedback.message = "%s drift" % str(value.agent_feedback.message)
		return {"ok": true, "status": 200, "headers": {}, "value": value} if run_id == str(run.run_id) else {"ok": false, "status": 404, "headers": {}, "error": {"code": "NOT_FOUND"}}

	func get_evidence(_attempt: Dictionary, evidence_id: String) -> Dictionary:
		evidence_reads += 1
		var value := evidence.duplicate(true)
		if drift_evidence:
			value.payload.intent_count = int(value.payload.intent_count) + 1
			var payload_sha := ContractValidator.canonical_json_sha256_v1(value.payload)
			value.integrity.payload_sha256 = payload_sha
			value.evidence_ref.sha256 = payload_sha
		return {"ok": true, "status": 200, "headers": {}, "value": value} if evidence_id == str(evidence.evidence_ref.evidence_id) else {"ok": false, "status": 404, "headers": {}, "error": {"code": "NOT_FOUND"}}

	func submit_agent_turn(_attempt: Dictionary, _session_id: String, _key: String, _request: Dictionary) -> Dictionary:
		mutation_attempts += 1
		return {"ok": false, "status": 0, "headers": {}, "error": {"code": "MUTATION_FORBIDDEN_DURING_RECOVERY"}}


func _initialize() -> void:
	_cleanup_persistence()
	_remove_autoload("SessionController")
	_remove_autoload("ClientStore")
	await process_frame

	var fixtures := _failure_fixtures()
	var build: Dictionary = fixtures.build
	var run: Dictionary = fixtures.run
	var evidence: Dictionary = fixtures.evidence
	var interaction: Dictionary = fixtures.interaction
	var bootstrap: Dictionary = fixtures.bootstrap
	var session: Dictionary = fixtures.session
	var objective: Dictionary = fixtures.objective
	var interaction_validation := ProductGatewayScript.new(null)._validate_interaction(
		interaction, str(session.session_id), str(interaction.interaction_id),
	)
	if (
		not ContractValidator.validate_skill_build(build).ok
		or not ContractValidator.validate_run(run).ok
		or not ContractValidator.validate_evidence(evidence).ok
		or not ContractValidator.validate_agent_session(session).ok
		or not interaction_validation.get("ok", false)
	):
		return _fail("Failed-restart fixtures are not closed public resources: build=%s run=%s evidence=%s session=%s interaction=%s" % [
			ContractValidator.validate_skill_build(build), ContractValidator.validate_run(run),
			ContractValidator.validate_evidence(evidence), ContractValidator.validate_agent_session(session),
			interaction_validation,
		])
	var refresh_guard := _verify_certified_build_authority_refresh(build, bootstrap, session)
	if not refresh_guard.is_empty():
		return _fail(refresh_guard)

	var initial := await _new_store(false)
	if not initial.configure_persistence(PERSISTENCE_PATH, true, false):
		return _fail("Could not enable isolated durable failure authority.")
	if not initial.bind_authority("https://api.yaya.example", bootstrap).get("ok", false):
		return _fail("Could not bind restart authority to the public origin/actor/content tuple.")
	initial.set_authoritative_bootstrap(bootstrap)
	initial.set_authoritative_session(session)
	initial.set_workspace({
		"session": {"session_id": session.session_id, "status": "ACTIVE", "last_turn_sequence": 4},
		"current_task": {"task_id": "task_patch_restart_0001"},
		"last_interaction_sequence": int(interaction.sequence),
	})
	initial.set_interaction_cursor(int(interaction.sequence))
	initial.set_objective_result(objective)
	if not initial.record_patch_failure_recovery_authority(build, run, interaction, [evidence], objective):
		return _fail("Exact certified Build/failed Run/Interaction/Evidence authority was not durably recorded.")
	var persisted_marker: Dictionary = initial.patch_failure_recovery_authority.duplicate(true)
	if (
		persisted_marker.is_empty()
		or str(persisted_marker.get("build_id", "")) != str(build.build_id)
		or str(persisted_marker.get("run_id", "")) != str(run.run_id)
		or str(persisted_marker.get("interaction_id", "")) != str(interaction.interaction_id)
		or persisted_marker.get("evidence_refs") != run.evidence_refs
	):
		return _fail("Durable marker omitted exact public failure identities.")
	_detach(initial)

	# Clean process recovery may restore the request entry only after public GETs
	# return the exact persisted certified Build, failed Run, Evidence, and
	# canonical visible Interaction. It performs no mutation.
	# A later non-eligible projection may advance the canonical page high-water
	# mark. Selection is the newest objective failure for the exact failed Run,
	# not an unsafe equality guess against the page cursor.
	var success := await _recover_once(build, run, evidence, interaction, bootstrap, session, [], "", true)
	var success_result: Dictionary = success.result
	var success_store: WalnutClientStore = success.store
	var success_controller: Node = success.controller
	var success_game: Game = success.game
	if (
		not success_result.get("ok", false)
		or not bool(success_result.get("value", {}).get("recovered", false))
		or not success_controller.can_request_ai_patch()
		or success_controller.certified_build != build
		or success_store.objective_result != objective
		or success_game.build_reads != 1
		or success_game.run_reads != 1
		or success_game.evidence_reads != 1
		or success_game.mutation_attempts != 0
	):
		return _fail("Clean restart did not re-close exact failure authority through GET-only recovery: %s" % success_result)
	_detach(success_controller)
	_detach(success_store)

	# Missing, drifted, or damaged authority remains durable for diagnosis but
	# cannot restore the button or emit a new request mutation.
	for corruption in ["INTERACTION_MISSING", "BUILD_MISSING", "RUN_DRIFT", "EVIDENCE_DRIFT"]:
		var interactions: Array[Dictionary] = [interaction]
		if corruption == "INTERACTION_MISSING":
			interactions.clear()
		var recovered := await _recover_once(build, run, evidence, interaction, bootstrap, session, interactions, corruption)
		var result: Dictionary = recovered.result
		var controller: Node = recovered.controller
		var game: Game = recovered.game
		var expected_code: String = {
			"INTERACTION_MISSING": "SKILL_PATCH_FAILURE_INTERACTION_DRIFT",
			"BUILD_MISSING": "SKILL_PATCH_FAILURE_BUILD_READ_FAILED",
			"RUN_DRIFT": "SKILL_PATCH_FAILURE_RUN_DRIFT",
			"EVIDENCE_DRIFT": "SKILL_PATCH_FAILURE_EVIDENCE_READ_FAILED",
		}[corruption]
		if (
			result.get("ok", true)
			or str(result.get("error", {}).get("code", "")) != expected_code
			or controller.can_request_ai_patch()
			or game.mutation_attempts != 0
			or recovered.store.patch_failure_recovery_authority.is_empty()
			or controller.patch_failure_recovery_result() != result
		):
			return _fail("%s did not fail closed with visible exact diagnostics and zero POST: %s" % [corruption, result])
		_detach(controller)
		_detach(recovered.store)

	_cleanup_persistence()
	print("SKILL_PATCH_VISIBLE_FAILURE_RESTART_TEST_PASS")
	quit(0)


func _verify_certified_build_authority_refresh(
	build: Dictionary,
	bootstrap: Dictionary,
	session: Dictionary,
) -> String:
	var store := StoreScript.new()
	store.name = "ClientStore"
	store.persistence_enabled = false
	root.add_child(store)
	var controller := ControllerScript.new()
	controller.name = "SessionController"
	root.add_child(controller)
	var draft: Dictionary = _example("product-skill-draft.json")
	draft.session_id = str(session.session_id)
	draft.draft_id = "draft_patch_restart_0001"
	draft.skill_id = str(build.skill_id)
	draft.revision = 1
	draft.request_context.actor = bootstrap.actor.duplicate(true)
	draft.request_context.content_ref = bootstrap.content.duplicate(true)
	draft.content_ref = bootstrap.content.duplicate(true)
	draft.links.self = "/product-experience/v1/sessions/%s/skill-drafts/%s" % [
		str(draft.session_id), str(draft.draft_id),
	]
	draft.links.session_workspace = "/product-experience/v1/sessions/%s/workspace" % str(draft.session_id)
	draft.draft_sha256 = ContractValidator.canonical_json_sha256_v1({
		"session_id": draft.session_id,
		"draft_id": draft.draft_id,
		"skill_id": draft.skill_id,
		"content_ref": draft.content_ref,
		"display_name": draft.display_name,
		"source_bundle": draft.source_bundle,
	})
	store.draft = draft.duplicate(true)
	store.draft_state = WalnutClientStore.DraftState.CLEAN
	store.authoritative_session = session.duplicate(true)
	var source_bundle_sha256 := str(controller.call(
		"_canonical_source_bundle_sha256", draft.source_bundle,
	))
	var refresh_build := build.duplicate(true)
	refresh_build.artifact.source_sha256 = source_bundle_sha256
	var draft_authority := {
		"build_id": str(refresh_build.build_id),
		"session_id": str(session.session_id),
		"draft_id": str(draft.draft_id),
		"skill_id": str(refresh_build.skill_id),
		"draft_revision": int(draft.revision),
		"draft_sha256": str(draft.draft_sha256),
		"source_bundle_sha256": source_bundle_sha256,
	}
	controller.configure_authority(bootstrap, session)
	controller.certified_build = refresh_build.duplicate(true)
	controller.certified_build_draft_authority = draft_authority.duplicate(true)
	controller.configure_authority(bootstrap.duplicate(true), session.duplicate(true))
	var failure := ""
	if (
		controller.certified_build != refresh_build
		or controller.certified_build_draft_authority != draft_authority
	):
		failure = "An exact Bootstrap refresh discarded the certified Build required to close the first objective failure."
	var advanced_session := session.duplicate(true)
	advanced_session.last_turn_sequence = int(session.last_turn_sequence) + 1
	advanced_session.updated_at = "2026-08-06T10:03:00Z"
	store.authoritative_session = advanced_session.duplicate(true)
	controller.configure_authority(bootstrap.duplicate(true), advanced_session)
	if failure.is_empty() and (
		controller.certified_build != refresh_build
		or controller.certified_build_draft_authority != draft_authority
	):
		failure = "A valid Session cursor/time advance discarded the still-exact certified Build authority."

	var coordinated_session_drifts := {
		"top_level_content": session.duplicate(true),
		"status": session.duplicate(true),
		"world": session.duplicate(true),
		"agent_profile": session.duplicate(true),
		"learner": session.duplicate(true),
		"channel": session.duplicate(true),
	}
	coordinated_session_drifts.top_level_content.content.content_hash = "f".repeat(64)
	coordinated_session_drifts.status.status = "CLOSED"
	coordinated_session_drifts.world.world_id = "world_patch_restart_drift"
	coordinated_session_drifts.agent_profile.agent_profile_id = "profile_patch_restart_drift"
	coordinated_session_drifts.learner.learner_id = "student_patch_restart_drift"
	coordinated_session_drifts.channel.channel = "TEACHER_PREVIEW"
	for drift_name in coordinated_session_drifts:
		if not failure.is_empty():
			break
		var coordinated_session: Dictionary = coordinated_session_drifts[drift_name]
		store.authoritative_session = coordinated_session.duplicate(true)
		controller.configure_authority(bootstrap.duplicate(true), coordinated_session)
		controller.certified_build = refresh_build.duplicate(true)
		controller.certified_build_draft_authority = draft_authority.duplicate(true)
		controller.configure_authority(bootstrap.duplicate(true), coordinated_session.duplicate(true))
		if (
			not controller.certified_build.is_empty()
			or not controller.certified_build_draft_authority.is_empty()
		):
			failure = "A coordinated Session authority drift (%s) retained a stale certified Build." % drift_name

	var drifts := {
		"actor": bootstrap.duplicate(true),
		"content": bootstrap.duplicate(true),
		"session": bootstrap.duplicate(true),
		"session_context": bootstrap.duplicate(true),
		"active": bootstrap.duplicate(true),
		"draft": bootstrap.duplicate(true),
	}
	drifts.actor.actor.actor_id = "student_patch_restart_drift"
	drifts.content.content.content_hash = "c".repeat(64)
	drifts.active.activation.active.activation_id = "activation_patch_restart_drift"
	for drift_name in drifts:
		if not failure.is_empty():
			break
		store.draft = draft.duplicate(true)
		store.authoritative_session = session.duplicate(true)
		controller.configure_authority(bootstrap, session)
		controller.certified_build = refresh_build.duplicate(true)
		controller.certified_build_draft_authority = draft_authority.duplicate(true)
		var next_session := session.duplicate(true)
		if drift_name == "session":
			next_session.session_id = "session_patch_restart_drift"
		if drift_name == "session_context":
			next_session.request_context.actor.actor_id = "student_patch_restart_context_drift"
		if drift_name == "draft":
			store.draft.draft_sha256 = "e".repeat(64)
		store.authoritative_session = next_session.duplicate(true)
		controller.configure_authority(drifts[drift_name], next_session)
		if (
			not controller.certified_build.is_empty()
			or not controller.certified_build_draft_authority.is_empty()
		):
			failure = "Authority drift (%s) retained a stale certified Build." % drift_name
	_detach(controller)
	_detach(store)
	return failure


func _recover_once(
	build: Dictionary,
	run: Dictionary,
	evidence: Dictionary,
	interaction: Dictionary,
	bootstrap: Dictionary,
	session: Dictionary,
	interactions: Array[Dictionary] = [],
	corruption := "",
	advance_cursor := false,
) -> Dictionary:
	var store := await _new_store(true)
	if not store.configure_persistence(PERSISTENCE_PATH, true, true):
		return {"result": {"ok": false, "error": {"code": "TEST_PERSISTENCE_LOAD_FAILED"}}, "store": store, "controller": Node.new(), "game": Game.new()}
	if advance_cursor:
		store.set_interaction_cursor(int(interaction.sequence) + 1)
	var game := Game.new()
	game.build = build
	game.run = run
	game.evidence = evidence
	game.missing_build = corruption == "BUILD_MISSING"
	game.drift_run = corruption == "RUN_DRIFT"
	game.drift_evidence = corruption == "EVIDENCE_DRIFT"
	var controller := ControllerScript.new()
	controller.name = "SessionController"
	root.add_child(controller)
	await process_frame
	controller.configure(game, null)
	controller.configure_authority(bootstrap, session)
	controller.configure_world_presentation(null, null, null, true)
	controller.configure_skill_patch_capability(_capability())
	var canonical: Array[Dictionary] = interactions
	if canonical.is_empty() and corruption != "INTERACTION_MISSING":
		canonical = [interaction]
	var result: Dictionary = await controller.recover_patch_failure_authority(canonical)
	return {"result": result, "store": store, "controller": controller, "game": game}


func _new_store(load_existing: bool) -> WalnutClientStore:
	var store := StoreScript.new()
	store.name = "ClientStore"
	store.persistence_enabled = false
	root.add_child(store)
	await process_frame
	if load_existing:
		# Loading is explicit below, after _ready cannot read the production path.
		store.persistence_enabled = false
	return store


func _failure_fixtures() -> Dictionary:
	var build: Dictionary = _example("game-skill-build.json")
	var run: Dictionary = _example("game-run.json")
	var evidence: Dictionary = _example("game-evidence.json")
	var interaction: Dictionary = _example("product-agent-interaction-page.json").interactions[0]

	run.status = "FAILED"
	run.world_application = {
		"status": "FAILED",
		"receipt": null,
		"failure": {
			"code": "WORLD_RULE_REJECTED",
			"category": "WORLD_RULE",
			"retryable": false,
			"user_message_key": "world.rule_rejected",
			"stage": "WORLD_COMMIT",
			"message": "The objective remained incomplete.",
		},
	}
	evidence.request_context.actor = run.request_context.actor.duplicate(true)
	evidence.request_context.content_ref = run.request_context.content_ref.duplicate(true)
	evidence.evidence_ref = {
		"evidence_id": "evidence_failed_run_restart_0001",
		"evidence_type": "SANDBOX_LOG",
		"created_at": str(run.updated_at),
		"sha256": "",
	}
	evidence.source = {
		"source_type": "SKILL_RUN",
		"source_id": str(run.run_id),
		"command_id": str(run.command_id),
		"world_id": null,
	}
	evidence.occurred_at = str(run.updated_at)
	evidence.recorded_at = str(run.updated_at)
	evidence.payload = {
		"evidence_kind": "SKILL_RUN",
		"run_id": str(run.run_id),
		"sandbox_status": str(run.sandbox.status),
		"world_status": str(run.world_application.status),
		"intent_count": run.sandbox.action_intents.size(),
	}
	var evidence_sha := ContractValidator.canonical_json_sha256_v1(evidence.payload)
	evidence.evidence_ref.sha256 = evidence_sha
	evidence.integrity = {"payload_sha256": evidence_sha, "previous_evidence_sha256": null}
	evidence.related_evidence = []
	run.evidence_refs = [evidence.evidence_ref.duplicate(true)]
	run.agent_feedback.evidence_refs = run.evidence_refs.duplicate(true)

	interaction.role = "bug_agent"
	interaction.response_type = "message"
	interaction.question = null
	interaction.hint_level = null
	interaction.skill_patch = null
	interaction.patch_decision = null
	interaction.feedback = run.agent_feedback.duplicate(true)
	interaction.feedback_event.feedback_sha256 = ContractValidator.canonical_json_sha256_v1(interaction.feedback)
	interaction.feedback_event.occurred_at = str(interaction.feedback.completed_at)
	interaction.links.skill_draft = null
	interaction.projection_source.role = "bug_agent"
	interaction.projection_source.response_type = "message"
	interaction.projection_source.question = null
	interaction.projection_source.hint_level = null
	interaction.projection_source.skill_patch_sha256 = null
	interaction.projection_source.feedback_sha256 = interaction.feedback_event.feedback_sha256
	var projection: Dictionary = interaction.projection_source.duplicate(true)
	projection.erase("source_sha256")
	interaction.projection_source.source_sha256 = ContractValidator.canonical_json_sha256_v1(projection)

	var active := {
		"activation_id": "activation_patch_restart_0001",
		"skill_id": str(build.skill_id),
		"skill_version_id": str(build.skill_version_id),
		"artifact_sha256": str(build.artifact.artifact_sha256),
		"certification_id": str(build.certification.certification_id),
		"registry_revision": 9,
		"activated_at": "2026-08-06T10:01:00Z",
	}
	var bootstrap := {
		"actor": build.request_context.actor.duplicate(true),
		"content": build.request_context.content_ref.duplicate(true),
		"world": {"world_id": "world_demo_001"},
		"activation": {
			"scope": {"world_id": "world_demo_001", "agent_profile_id": "profile_patch_restart_0001"},
			"registry_revision": 9,
			"active": active,
		},
	}
	var session: Dictionary = _example("game-agent-session.json")
	session.world_id = str(bootstrap.activation.scope.world_id)
	session.learner_id = str(bootstrap.actor.actor_id)
	session.agent_profile_id = str(bootstrap.activation.scope.agent_profile_id)
	session.channel = "GAME"
	var objective := {
		"summary": "The canonical objective failed.",
		"objective_succeeded": false,
		"run_id": str(run.run_id),
	}
	return {
		"build": build, "run": run, "evidence": evidence,
		"interaction": interaction, "bootstrap": bootstrap,
		"session": session, "objective": objective,
	}


func _capability() -> Dictionary:
	return {
		"world_presentation_enabled": true,
		"skill_patch_enabled": true,
		"skill_patch_constraints": {
			"request_mode": "EXPLICIT_UI_ACTION", "selection_target": "FAILED_INTERACTION",
			"agent_role": "teaching_agent", "scenario": "RECTIFICATION",
			"required_hint_level": 4, "operation": "UPSERT_FILE",
			"target": "CURRENT_ENTRYPOINT", "max_files": 1, "max_operations": 1,
			"requires_failed_evidence": true, "cas_required": true,
			"requires_student_confirmation": true, "auto_build": false,
			"auto_activate": false, "auto_run": false,
		},
	}


func _example(file_name: String) -> Dictionary:
	var examples := AgentContractFixtureLocator.examples_root()
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(examples.path_join(file_name)))
	return _normalize(parsed.value)


func _normalize(value: Variant) -> Variant:
	if typeof(value) == TYPE_FLOAT and value == floor(value):
		return int(value)
	if value is Array:
		var result: Array = []
		for item in value:
			result.append(_normalize(item))
		return result
	if value is Dictionary:
		var result := {}
		for key in value:
			result[key] = _normalize(value[key])
		return result
	return value


func _remove_autoload(name: String) -> void:
	var node := root.get_node_or_null(name)
	if node != null:
		root.remove_child(node)
		node.free()


func _detach(node: Node) -> void:
	if is_instance_valid(node) and node.get_parent() == root:
		root.remove_child(node)
		node.free()


func _cleanup_persistence() -> void:
	for candidate in [PERSISTENCE_PATH, "%s.tmp" % PERSISTENCE_PATH, "%s.bak" % PERSISTENCE_PATH]:
		if FileAccess.file_exists(candidate):
			DirAccess.remove_absolute(ProjectSettings.globalize_path(candidate))


func _fail(message: String) -> void:
	_cleanup_persistence()
	push_error(message)
	quit(1)
