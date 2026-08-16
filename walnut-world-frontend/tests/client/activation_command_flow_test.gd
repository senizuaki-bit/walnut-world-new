extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")

class Game:
	extends RefCounted
	var activation_request: Dictionary = {}
	func activate_skill_version(_attempt: Dictionary, _version: String, _key: String, request: Dictionary) -> Dictionary:
		activation_request = request.duplicate(true)
		return {"ok": true, "value": {"command_id": "cmd_activation_0001"}}
	func get_command(_attempt: Dictionary, command_id: String) -> Dictionary:
		return {"ok": true, "value": {"command_id": command_id, "terminal": true, "status": "APPLIED", "result": {"resource_type": "SKILL_ACTIVATION", "resource_id": "activation_demo_0001"}}}
	func get_skill_activation(_attempt: Dictionary, _activation_id: String) -> Dictionary:
		return {"ok": true, "value": {"activation_id": "activation_demo_0001", "skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "certification_id": "cert_demo_0001", "artifact_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "activation_scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "previous_registry_revision": 7, "registry_revision": 8, "activated_at": "2026-08-12T00:00:00Z"}}

func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null:
		store = StoreScript.new(); store.name = "ClientStore"; root.add_child(store)
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new(); controller.name = "SessionController"; root.add_child(controller)
	await process_frame
	store.persistence_enabled = false
	var game := Game.new()
	controller.configure(game)
	controller.configure_polling({"initial_delay_seconds": 0.0, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_ratio": 0.0})
	var bootstrap := _bootstrap()
	store.set_authoritative_bootstrap(bootstrap)
	controller.configure_authority(bootstrap)
	controller.certified_build = {"skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "artifact": {"artifact_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}, "certification": {"certification_id": "cert_demo_0001"}}
	await controller.request_activation()
	if (
		store.flow_state != WalnutClientStore.FlowState.ACTIVE
		or int(game.activation_request.get("expected_registry_revision", -1)) != 7
		or str(controller.active_activation.get("activation_id", "")) != "activation_demo_0001"
		or store.active_skill_tuple != controller.active_skill_tuple
		or int(store.activation_authority.get("registry_revision", -1)) != 8
	):
		push_error("Activation must reconcile a matching canonical SkillActivation before entering ACTIVE.")
		quit(1); return
	print("ACTIVATION_COMMAND_FLOW_TEST_PASS")
	quit(0)


func _bootstrap() -> Dictionary:
	return {
		"actor": {
			"tenant_id": "tenant_demo",
			"actor_id": "learner_demo_0001",
			"actor_type": "student",
			"roles": ["student"],
		},
		"content": {
			"unit_id": "TASK_DEMO_001",
			"version": "1.0.0",
			"content_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		},
		"activation": {
			"scope": {
				"world_id": "world_demo_0001",
				"agent_profile_id": "profile_demo_0001",
			},
			"registry_revision": 7,
			"active": null,
		},
	}
