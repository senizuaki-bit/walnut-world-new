class_name AgentInteractionPresenter
extends CanvasLayer

signal role_presentation_started(role_id: StringName, interaction_id: String)
signal role_presentation_finished(role_id: StringName, interaction_id: String)
signal world_cue_requested(presentation_key: StringName, active: bool)
signal presentation_rejected(reason: String)

@export var character_catalog: AgentCharacterCatalog

@onready var overlay: StoryDialogueOverlay = $StoryDialogueOverlay

var _pending: Array[Dictionary] = []
var _presented_interaction_ids: Dictionary = {}
var _active_interaction: Dictionary = {}


func _ready() -> void:
	if character_catalog == null:
		push_error("AgentInteractionPresenter requires an AgentCharacterCatalog.")
		return
	var validation := character_catalog.validate()
	if not bool(validation.get("ok", false)):
		push_error("AgentInteractionPresenter catalog is invalid: %s" % validation.get("message", ""))
	overlay.sequence_finished.connect(_on_sequence_finished)


func display_name_for(role_id: StringName) -> String:
	if role_id == &"system":
		return "系统"
	var profile := character_catalog.profile_for(role_id) if character_catalog != null else null
	return profile.display_name if profile != null else "系统"


func enqueue_interaction(interaction: Dictionary) -> bool:
	if character_catalog == null:
		return _reject("Agent character catalog is unavailable.")
	var interaction_id := str(interaction.get("interaction_id", ""))
	var role_id := StringName(str(interaction.get("role", "")))
	var response_type := str(interaction.get("response_type", ""))
	if interaction_id.is_empty():
		return _reject("AgentInteraction is missing interaction_id.")
	if _presented_interaction_ids.has(interaction_id) or _contains_pending(interaction_id):
		return false
	if role_id == &"system" or response_type in ["skill_patch", "patch"]:
		return false
	var profile := character_catalog.profile_for(role_id)
	if profile == null:
		return _reject("AgentInteraction uses an unknown character role: %s" % role_id)
	var feedback: Variant = interaction.get("feedback")
	if not feedback is Dictionary or str(feedback.get("message", "")).is_empty():
		return _reject("AgentInteraction has no presentable feedback message.")
	_pending.append(interaction.duplicate(true))
	if _active_interaction.is_empty():
		_present_next()
	return true


func pending_count() -> int:
	return _pending.size()


func is_presenting() -> bool:
	return not _active_interaction.is_empty()


func clear_queue() -> void:
	_pending.clear()
	if overlay.visible:
		overlay.skip_sequence()


func _present_next() -> void:
	if _pending.is_empty():
		return
	_active_interaction = _pending.pop_front()
	var interaction_id := str(_active_interaction.interaction_id)
	var role_id := StringName(str(_active_interaction.role))
	var profile := character_catalog.profile_for(role_id)
	if profile == null:
		_active_interaction.clear()
		_reject("Queued AgentInteraction lost its character profile.")
		_present_next()
		return
	_presented_interaction_ids[interaction_id] = true
	var feedback: Dictionary = _active_interaction.feedback
	var question_value: Variant = _active_interaction.get("question")
	var question := "" if question_value == null else str(question_value)
	overlay.play_agent_presentation(
		profile.display_name,
		profile.portrait,
		str(feedback.message),
		question,
		_response_label(str(_active_interaction.response_type), _active_interaction.get("hint_level")),
	)
	role_presentation_started.emit(role_id, interaction_id)
	world_cue_requested.emit(profile.world_presentation_key, true)


func _on_sequence_finished() -> void:
	if _active_interaction.is_empty():
		return
	var interaction_id := str(_active_interaction.interaction_id)
	var role_id := StringName(str(_active_interaction.role))
	var profile := character_catalog.profile_for(role_id)
	if profile != null:
		world_cue_requested.emit(profile.world_presentation_key, false)
	role_presentation_finished.emit(role_id, interaction_id)
	_active_interaction.clear()
	_present_next()


func _contains_pending(interaction_id: String) -> bool:
	if str(_active_interaction.get("interaction_id", "")) == interaction_id:
		return true
	for interaction in _pending:
		if str(interaction.get("interaction_id", "")) == interaction_id:
			return true
	return false


func _reject(reason: String) -> bool:
	presentation_rejected.emit(reason)
	push_error("AgentInteractionPresenter: %s" % reason)
	return false


func _response_label(response_type: String, hint_level_value: Variant) -> String:
	match response_type:
		"question":
			return "追问"
		"hint":
			var hint_level := int(hint_level_value) if typeof(hint_level_value) == TYPE_INT else 0
			return {0: "观察", 1: "方向提示", 2: "概念提示", 3: "修改建议", 4: "AI 协助修改"}.get(clampi(hint_level, 0, 4), "提示")
		"growth_summary":
			return "成长总结"
		_:
			return "任务说明"
