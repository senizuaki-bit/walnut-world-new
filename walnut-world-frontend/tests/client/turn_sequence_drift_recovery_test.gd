extends SceneTree

## Regression: a stale client_turn_sequence must not disable the student forever.
##
## The gateway accepts only the next client_turn_sequence. This client derives it
## from its own Workspace copy, so the moment that copy falls behind the Session
## -- another window, a lost update, any Turn raised outside this client -- every
## later action recomputes the same stale number from the same stale Workspace
## and is refused again. The refusal happens during acceptance, so no Command is
## ever created and the student only sees "这次没有连上" with nothing to act on.
##
## The fix re-reads ONLY the Session's Turn cursor and retries once. Pinned here:
##   1. drift is corrected and the retry actually reaches the gateway
##   2. the retry carries the corrected number, not the stale one
##   3. NO retry when the cursor is unchanged -- the real refusal must surface
##      rather than being masked by a second identical attempt
##   4. the Draft is never touched, because the student may have unsaved edits

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")

const STALE_CURSOR := 3
const SERVER_CURSOR := 7


class Game:
	extends RefCounted
	var accepted_sequences: Array[int] = []
	var refused_sequences: Array[int] = []
	var server_cursor := SERVER_CURSOR

	func submit_agent_turn(_a: Dictionary, _s: String, _k: String, request: Dictionary) -> Dictionary:
		var sequence := int(request.client_state.client_turn_sequence)
		if sequence != server_cursor + 1:
			refused_sequences.append(sequence)
			return {
				"ok": false,
				"status": 400,
				"headers": {},
				"error": {
					"scope": "HTTP",
					"code": "INVALID_REQUEST",
					"message": "client_turn_sequence must be the next session sequence",
					"retryable": false,
				},
			}
		accepted_sequences.append(sequence)
		# Accepting is enough for this test; the envelope executor then fails on
		# the command read, which does not affect what is being pinned here.
		return {"ok": true, "status": 202, "headers": {}, "value": {"command_id": "cmd_drift_0001"}}

	func get_command(_a: Dictionary, command_id: String) -> Dictionary:
		return {"ok": true, "value": {"command_id": command_id, "terminal": true, "status": "APPLIED", "links": {}}}

	func get_run(_a: Dictionary, _run_id: String) -> Dictionary:
		return {"ok": false}


class Product:
	extends RefCounted
	var workspace_reads := 0
	var draft_reads := 0
	var cursor := SERVER_CURSOR

	func get_workspace(_a: Dictionary, session_id: String) -> Dictionary:
		workspace_reads += 1
		return {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {
				"session": {
					"session_id": session_id,
					"status": "ACTIVE",
					"last_turn_sequence": cursor,
				},
				"current_task": {"task_id": "task_demo_0001"},
			},
		}

	func get_draft(_a: Dictionary, _s: String, _d: String) -> Dictionary:
		draft_reads += 1
		return {"ok": false}

	func list_interactions(_a: Dictionary, _s: String, after_sequence: int, _l: int) -> Dictionary:
		return {
			"ok": true,
			"value": {
				"interactions": [],
				"next_after_sequence": after_sequence,
				"high_watermark_sequence": after_sequence,
				"has_more": false,
			},
		}


func _initialize() -> void:
	var failures: Array[String] = []

	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null:
		store = StoreScript.new()
		store.name = "ClientStore"
		root.add_child(store)
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new()
		controller.name = "SessionController"
		root.add_child(controller)
	await process_frame
	store.persistence_enabled = false

	var bootstrap := {
		"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]},
		"content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": "d".repeat(64)},
		"session": {"current_session_id": "session_demo_0001"},
		"activation": {"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "registry_revision": 0, "active": null},
	}
	var session := {"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": bootstrap.content}
	store.set_authoritative_bootstrap(bootstrap)
	store.set_authoritative_session(session)
	# The client's Workspace copy is behind the Session on purpose.
	store.set_workspace({
		"session": {"session_id": "session_demo_0001", "status": "ACTIVE", "last_turn_sequence": STALE_CURSOR},
		"current_task": {"task_id": "task_demo_0001"},
	})
	store.replace_world({"world_id": "world_demo_0001", "revision": 4, "last_event_sequence": 7, "state_schema_version": "1.0.0", "state_hash": "a".repeat(64), "world_rules_version": "rules", "state": {}})

	var game := Game.new()
	var product := Product.new()
	controller.configure(game, product)
	controller.configure_authority(bootstrap, session)
	controller.configure_polling({"initial_delay_seconds": 0.0, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_ratio": 0.0, "interaction_delay_seconds": 0.0, "interaction_deadline_seconds": 0.2})
	controller.configure_draft_context({"attempt": {}})
	store.set_flow(WalnutClientStore.FlowState.ACTIVE)

	await controller.request_hint()

	if game.refused_sequences != [STALE_CURSOR + 1]:
		failures.append("失败前提不成立：第一次提交必须带着过期序号 %d 被网关拒绝，实际 %s" % [
			STALE_CURSOR + 1, str(game.refused_sequences),
		])
	if game.accepted_sequences != [SERVER_CURSOR + 1]:
		failures.append("序号漂移后必须用重新读到的序号 %d 重试一次，实际 %s" % [
			SERVER_CURSOR + 1, str(game.accepted_sequences),
		])
	if product.draft_reads != 0:
		failures.append("纠正序号不得顺带重读 Draft：学生编辑器里可能有未保存的改动。")
	if int(store.workspace.session.last_turn_sequence) != STALE_CURSOR:
		failures.append("纠正序号只用于这一次重试，不得写回本地 Workspace 权威。")

	# No drift: the refusal is real and must surface, not be retried away.
	# The accepted attempt above leaves its envelope open (its Command never
	# closes in this fixture), and reconciling that is a different path than the
	# one under test, so start this scenario from a clean slot.
	store.clear_pending_operation("agent_hint")
	game.accepted_sequences.clear()
	game.refused_sequences.clear()
	var reads_before := product.workspace_reads
	# Refuse everything, but report a cursor that agrees with this client: there
	# is no drift to correct, so the refusal must be surfaced as-is.
	game.server_cursor = 99
	product.cursor = STALE_CURSOR
	store.set_flow(WalnutClientStore.FlowState.ACTIVE)
	await controller.request_hint()
	if not game.accepted_sequences.is_empty():
		failures.append("失败前提不成立：这一轮不应有被接受的提交。")
	if game.refused_sequences.size() != 1:
		failures.append("没有漂移时只能提交一次，不得掩盖真实拒绝原因，实际提交 %d 次" % game.refused_sequences.size())
	if product.workspace_reads <= reads_before:
		failures.append("失败前提不成立：仍应重读一次游标来判断是否漂移。")

	if failures.is_empty():
		print("TURN_SEQUENCE_DRIFT_RECOVERY_TEST_PASS")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
