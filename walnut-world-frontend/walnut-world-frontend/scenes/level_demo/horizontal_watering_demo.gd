class_name HorizontalWateringDemo
extends Node3D

signal draft_changed(source: String)
signal submit_requested(source: String)
signal hint_requested(message: String)

enum Phase { CONNECTING, READY, SUBMITTING, FAILED, COMPLETED }

const HARVEST_EVENT_TYPE := "world.action.harvested"
const HARVEST_EVENT_VERSION := 1
const HARVEST_DURATION_SECONDS := 0.72
const AGENT_CHARACTER_CATALOG := preload("res://resources/agent/agent_character_catalog.tres")

@export_range(0.0, 2.0, 0.05) var demo_timing_scale: float = 1.0
@export var start_on_ready: bool = true

@onready var hud: CanvasLayer = $Hud
@onready var manual_row: Node3D = $ManualRow
@onready var auto_row: Node3D = $AutoRow
@onready var little_walnut: Sprite3D = $Cast/LittleWalnut
@onready var bug_character: Node3D = $Cast/Bug
@onready var shu_shu: AnimatedSprite3D = $Cast/ShuShu
@onready var manual_watering_can: AnimatedSprite3D = $ManualWateringCan
@onready var magic_watering_can: AnimatedSprite3D = $MagicWateringCan
@onready var manual_path: Label3D = $Guidance/ManualPath
@onready var variable_badge: Label3D = $Guidance/VariableBadge
@onready var task_card: Control = $Hud/SafeArea/TaskCard
@onready var task_title: Label = $Hud/SafeArea/TaskCard/Margin/Content/TaskTitle
@onready var task_progress: Label = $Hud/SafeArea/TaskCard/Margin/Content/TaskProgress
@onready var phase_strip: Control = $Hud/SafeArea/PhaseStrip
@onready var manual_phase_label: Label = $Hud/SafeArea/PhaseStrip/Margin/Phases/ManualPhase
@onready var loop_phase_label: Label = $Hud/SafeArea/PhaseStrip/Margin/Phases/LoopPhase
@onready var run_phase_label: Label = $Hud/SafeArea/PhaseStrip/Margin/Phases/RunPhase
@onready var hint_button: Button = $Hud/SafeArea/ToolRail/HintButton
@onready var tool_rail: Control = $Hud/SafeArea/ToolRail
@onready var completion_card: Control = $Hud/SafeArea/CompletionCard
@onready var completion_title: Label = $Hud/SafeArea/CompletionCard/Margin/Content/Title
@onready var completion_summary: Label = $Hud/SafeArea/CompletionCard/Margin/Content/SkillSaved
@onready var completion_detail: Label = $Hud/SafeArea/CompletionCard/Margin/Content/NextTech
@onready var growth_feedback: Control = $Hud/SafeArea/GrowthFeedback
@onready var code_drawer: Control = $Hud/CodeDrawer
@onready var drawer_save_state: Label = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/DrawerHeader/SaveState
@onready var editor: CodeEdit = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeEditor
@onready var start_input: LineEdit = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeFillPanel/CodeMargin/CodeLines/LoopLine/StartInput
@onready var limit_input: LineEdit = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeFillPanel/CodeMargin/CodeLines/LoopLine/LimitInput
@onready var index_input: LineEdit = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeFillPanel/CodeMargin/CodeLines/OutputLine/IndexInput
@onready var trace_label: RichTextLabel = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Trace
@onready var reset_button: Button = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Actions/ResetButton
@onready var submit_button: Button = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Actions/SubmitButton
@onready var spell_overlay: Control = $Hud/MagicSpellOverlay
@onready var story_dialogue: StoryDialogueOverlay = $Hud/StoryDialogueOverlay
@onready var agent_status_panel: PanelContainer = $Hud/AgentStatusPanel
@onready var agent_status_title: Label = $Hud/AgentStatusPanel/Margin/Content/Title
@onready var agent_status_body: Label = $Hud/AgentStatusPanel/Margin/Content/Body
@onready var recovery_panel: Control = $Hud/RecoveryPanel
@onready var recovery_message: Label = $Hud/RecoveryPanel/Panel/Margin/Content/Message
@onready var autosave_timer: Timer = $AutoSaveTimer
@onready var save_indicator_timer: Timer = $SaveIndicatorTimer

var _phase := Phase.CONNECTING
var _demo_active := false
var _run_generation := 0
var _agent_mode := false
var _authoritative_draft_loaded := false
var _authoritative_source := ""
var _current_source := ""
var _authoritative_story: Array[String] = []
var _authoritative_success_story := ""
var _synchronizing_draft := false
var _recovery_active := false
var _plot_by_id: Dictionary = {}
var _plot_position_by_id: Dictionary = {}
var _snapshot_fingerprint := ""
var _presenting_plot_id := ""
var _presenting_plot: WateringPlot
var _watering_tween: Tween
var _hud_intro_tween: Tween
var _little_walnut_rest_position := Vector3.ZERO
var _task_card_rest_position := Vector2.ZERO


func _ready() -> void:
	hint_button.pressed.connect(_on_hint_button_pressed)
	reset_button.pressed.connect(_on_reset_button_pressed)
	submit_button.pressed.connect(_on_submit_button_pressed)
	editor.text_changed.connect(_on_editor_text_changed)
	autosave_timer.timeout.connect(_emit_draft_change)
	save_indicator_timer.timeout.connect(func() -> void: drawer_save_state.visible = false)
	_little_walnut_rest_position = little_walnut.position
	_task_card_rest_position = task_card.position
	_reset_visual_state()
	if start_on_ready:
		start_level()
	else:
		set_demo_active(false)


func start_level() -> void:
	_run_generation += 1
	set_demo_active(true)
	_reset_visual_state()
	_show_code_drawer()
	_play_hud_intro()
	_play_configured_story()


func set_demo_active(active: bool) -> void:
	_demo_active = active
	if not active:
		_run_generation += 1
		_stop_activity()
	visible = active
	hud.visible = active
	process_mode = Node.PROCESS_MODE_INHERIT if active else Node.PROCESS_MODE_DISABLED
	set_process(active)
	set_physics_process(active)
	set_process_input(active)
	set_process_unhandled_input(active)


func load_agent_content(content: Dictionary) -> bool:
	return configure_authoritative_content(content)


func configure_authoritative_content(content: Dictionary) -> bool:
	var task_value: Variant = content.get("task")
	if not task_value is Dictionary:
		_clear_authoritative_content()
		_show_recovery("Content task 无法映射")
		return false
	var task: Dictionary = task_value
	var name_value: Variant = task.get("name")
	var goal_value: Variant = task.get("goal")
	var instructions_value: Variant = task.get("instructions")
	if typeof(name_value) != TYPE_STRING or String(name_value).is_empty():
		_clear_authoritative_content()
		_show_recovery("Content task.name 无法映射")
		return false
	if typeof(goal_value) != TYPE_STRING or String(goal_value).is_empty():
		_clear_authoritative_content()
		_show_recovery("Content task.goal 无法映射")
		return false
	if not instructions_value is Array or instructions_value.is_empty() or instructions_value.size() > 32:
		_clear_authoritative_content()
		_show_recovery("Content task.instructions 无法映射")
		return false
	var instruction_lines: Array[String] = []
	for instruction_value in instructions_value:
		if typeof(instruction_value) != TYPE_STRING or String(instruction_value).is_empty():
			_clear_authoritative_content()
			_show_recovery("Content task.instructions 无法映射")
			return false
		instruction_lines.append(String(instruction_value))
	task_title.text = String(name_value)
	task_progress.text = String(goal_value)
	_authoritative_story.clear()
	_authoritative_success_story = ""
	var story_value: Variant = task.get("story")
	if story_value is Dictionary:
		var story: Dictionary = story_value
		var opening_value: Variant = story.get("opening")
		var success_value: Variant = story.get("success")
		if typeof(opening_value) == TYPE_STRING and not String(opening_value).is_empty():
			_authoritative_story.append(String(opening_value))
		if typeof(success_value) == TYPE_STRING and not String(success_value).is_empty():
			_authoritative_success_story = String(success_value)
	var status_lines := instruction_lines.duplicate()
	if not _authoritative_story.is_empty():
		status_lines.append(_authoritative_story.front())
	_show_agent_status(task_title.text, "\n".join(status_lines))
	_recovery_active = false
	recovery_panel.visible = false
	return true


func configure_agent_mode(enabled: bool) -> void:
	_agent_mode = enabled
	_authoritative_draft_loaded = false
	_set_phase(Phase.CONNECTING)
	agent_status_panel.visible = true
	agent_status_title.text = "正在连接"
	agent_status_body.text = ""
	_update_authoritative_actions()


func load_agent_draft(source: String) -> void:
	if not _agent_mode:
		return
	_authoritative_source = source
	_current_source = source
	_authoritative_draft_loaded = true
	_synchronizing_draft = true
	editor.text = source
	_synchronizing_draft = false
	trace_label.text = source
	drawer_save_state.visible = true
	drawer_save_state.text = "✓"
	_set_phase(Phase.READY)
	agent_status_title.text = "Draft"
	agent_status_body.text = ""
	_update_authoritative_actions()


func update_agent_draft_state(state: int) -> void:
	if not _agent_mode:
		return
	if state == WalnutClientStore.DraftState.CLEAN:
		_authoritative_source = _current_source
	drawer_save_state.visible = true
	drawer_save_state.text = {
		WalnutClientStore.DraftState.CLEAN: "✓", WalnutClientStore.DraftState.DIRTY: "•",
		WalnutClientStore.DraftState.SAVING: "↻", WalnutClientStore.DraftState.CONFLICT: "!",
		WalnutClientStore.DraftState.SAVE_FAILED: "!",
	}.get(state, "•")
	_update_authoritative_actions(state not in [WalnutClientStore.DraftState.SAVING, WalnutClientStore.DraftState.CONFLICT])


func begin_agent_submission(message: String) -> void:
	_set_phase(Phase.SUBMITTING)
	_show_agent_status("Run + Evidence", message)
	_update_authoritative_actions(false)


func update_agent_submission_stage(message: String, keep_running: bool = true) -> void:
	if keep_running:
		_set_phase(Phase.SUBMITTING)
	_show_agent_status("Run + Evidence", message)


func present_agent_interactions(interactions: Array) -> void:
	if interactions.is_empty():
		return
	var latest: Variant = interactions.back()
	if not latest is Dictionary:
		return
	var interaction: Dictionary = latest
	var role_id := StringName(str(interaction.get("role", "")))
	var feedback_value: Variant = interaction.get("feedback")
	if role_id.is_empty() or not feedback_value is Dictionary:
		return
	var feedback: Dictionary = feedback_value
	var message_value: Variant = feedback.get("message")
	var response_type_value: Variant = interaction.get("response_type")
	if typeof(message_value) != TYPE_STRING or String(message_value).is_empty() or typeof(response_type_value) != TYPE_STRING:
		return
	var message := String(message_value)
	var response_type := String(response_type_value)
	var hint_value: Variant = interaction.get("hint_level")
	var question_value: Variant = interaction.get("question")
	if response_type not in ["message", "question", "hint", "growth_summary"]:
		return
	if (
		response_type == "hint"
		and (
			typeof(hint_value) != TYPE_INT
			or int(hint_value) < 0
			or int(hint_value) > 3
			or question_value != null
		)
	):
		return
	if response_type == "question" and (typeof(question_value) != TYPE_STRING or String(question_value).is_empty() or hint_value != null):
		return
	if response_type in ["message", "growth_summary"] and (hint_value != null or question_value != null):
		return
	var suffix := " · L%d" % int(hint_value) if response_type == "hint" else ""
	var profile = AGENT_CHARACTER_CATALOG.profile_for(role_id)
	_show_agent_status("%s%s" % [str(profile.display_name) if profile != null else str(role_id), suffix], message)
	if profile != null:
		story_dialogue.play_sequence(profile.display_name, profile.portrait, [message])


func present_agent_error(message: String) -> void:
	_show_recovery(message)


func fail_agent_submission(stage: String, message: String) -> void:
	_set_phase(Phase.FAILED)
	_show_agent_status(stage, message)
	_update_authoritative_actions(_authoritative_draft_loaded)


func complete_agent_submission(summary: String) -> void:
	_set_phase(Phase.COMPLETED)
	completion_card.visible = true
	completion_title.text = _authoritative_success_story if not _authoritative_success_story.is_empty() else "任务已完成"
	completion_summary.text = summary if not summary.is_empty() else "权威运行已确认完成"
	completion_detail.text = ""
	_show_agent_status("Run + Evidence", summary)
	_update_authoritative_actions(false)


func replace_authoritative_world(snapshot: Dictionary) -> bool:
	return bind_authoritative_snapshot(snapshot)


func bind_authoritative_snapshot(snapshot: Dictionary) -> bool:
	var state_value: Variant = snapshot.get("state")
	if not state_value is Dictionary:
		return _reject_world_projection("Snapshot state 无法映射")
	var plots_value: Variant = (state_value as Dictionary).get("plots")
	if not plots_value is Array:
		return _reject_world_projection("Snapshot plots 无法映射")
	var sortable: Array[Dictionary] = []
	var seen_ids: Dictionary = {}
	for value in plots_value:
		if not value is Dictionary:
			return _reject_world_projection("Snapshot plot 结构无法映射")
		var plot: Dictionary = value
		var plot_fields := ["plot_id", "position", "soil_state", "hydration", "crop", "last_updated_event_sequence"]
		if plot.size() != plot_fields.size():
			return _reject_world_projection("Snapshot plot 结构无法映射")
		for field in plot_fields:
			if not plot.has(field):
				return _reject_world_projection("Snapshot plot 结构无法映射")
		var plot_id := str(plot.get("plot_id", ""))
		var position_value: Variant = plot.get("position")
		if plot_id.is_empty() or not position_value is Dictionary or seen_ids.has(plot_id):
			return _reject_world_projection("Snapshot plot_id 无法映射")
		var position: Dictionary = position_value
		if position.size() != 2 or typeof(position.get("x")) != TYPE_INT or typeof(position.get("y")) != TYPE_INT:
			return _reject_world_projection("Snapshot position 无法映射")
		var soil_state_value: Variant = plot.get("soil_state")
		var hydration_value: Variant = plot.get("hydration")
		var crop_value: Variant = plot.get("crop")
		var last_updated_value: Variant = plot.get("last_updated_event_sequence")
		if typeof(soil_state_value) != TYPE_STRING or String(soil_state_value) not in ["UNTILLED", "TILLED"]:
			return _reject_world_projection("Snapshot soil_state 无法映射")
		if typeof(hydration_value) != TYPE_INT or int(hydration_value) < 0 or int(hydration_value) > 10000:
			return _reject_world_projection("Snapshot hydration 无法映射")
		if not _valid_snapshot_crop(crop_value):
			return _reject_world_projection("Snapshot crop 无法映射")
		if typeof(last_updated_value) != TYPE_INT or int(last_updated_value) < 0:
			return _reject_world_projection("Snapshot last_updated_event_sequence 无法映射")
		seen_ids[plot_id] = true
		sortable.append({
			"plot_id": plot_id, "x": int(position.x), "y": int(position.y),
			"watered": int(hydration_value) > 0,
		})
	sortable.sort_custom(func(left: Dictionary, right: Dictionary) -> bool:
		if left.x != right.x:
			return left.x < right.x
		if left.y != right.y:
			return left.y < right.y
		return left.plot_id < right.plot_id
	)
	if sortable.size() != auto_row.get_child_count():
		return _reject_world_projection("Snapshot plots 与预置土地不匹配")
	_stop_watering_presentation()
	_presenting_plot_id = ""
	_presenting_plot = null
	_plot_by_id.clear()
	_plot_position_by_id.clear()
	for index in sortable.size():
		var plot_node := auto_row.get_child(index) as WateringPlot
		var plot_id := str(sortable[index].plot_id)
		_plot_by_id[plot_id] = plot_node
		_plot_position_by_id[plot_id] = Vector2i(int(sortable[index].x), int(sortable[index].y))
		plot_node.set_watered(bool(sortable[index].watered), false)
	_snapshot_fingerprint = "%s:%s:%s:%s" % [
		str(snapshot.get("world_id", "")), int(snapshot.get("revision", -1)),
		int(snapshot.get("last_event_sequence", -1)), str(snapshot.get("state_hash", "")),
	]
	auto_row.visible = true
	_recovery_active = false
	recovery_panel.visible = false
	_show_agent_status("World Snapshot", _snapshot_fingerprint)
	return true


func present_verified_world_event(event: Dictionary, speed: float = 1.0) -> Dictionary:
	if speed not in [1.0, 2.0] or not _valid_harvest_event(event):
		_show_recovery("权威世界事件无法播放")
		return {"ok": false, "duration_seconds": 0.0}
	var payload: Dictionary = event.payload
	var plot_id := str(payload.plot_id)
	var plot := _plot_by_id.get(plot_id) as WateringPlot
	var event_position := Vector2i(int(payload.position.x), int(payload.position.y))
	if plot == null or _plot_position_by_id.get(plot_id) != event_position:
		_show_recovery("权威 plot_id/position 无预置映射")
		return {"ok": false, "duration_seconds": 0.0}
	_stop_watering_presentation()
	_presenting_plot_id = plot_id
	_presenting_plot = plot
	_play_verified_watering(plot, plot_id, speed, _run_generation)
	return {"ok": true, "duration_seconds": HARVEST_DURATION_SECONDS}


func finish_verified_world_event(_event: Dictionary, skipped: bool = false) -> bool:
	if not skipped and _presenting_plot != null:
		_presenting_plot.set_watered(true, true)
	_stop_watering_presentation()
	_presenting_plot_id = ""
	_presenting_plot = null
	agent_status_title.text = "World Snapshot"
	return true


func present_world_playback_state(state: String) -> void:
	_show_agent_status("World Snapshot", state)


func require_world_playback_recovery(after_sequence: int) -> void:
	_show_recovery("需要从事件 %d 后恢复" % after_sequence)


func get_fill_values() -> Array[String]:
	return [start_input.text, limit_input.text, index_input.text]


func get_saved_draft() -> String:
	return _current_source


func get_phase_name() -> String:
	return Phase.keys()[_phase]


func is_story_dialogue_visible() -> bool:
	return story_dialogue.visible


func skip_story_dialogue() -> void:
	story_dialogue.skip_sequence()


func _reset_visual_state() -> void:
	_stop_activity()
	for row in [manual_row, auto_row]:
		for child in row.get_children():
			var plot := child as WateringPlot
			plot.set_interactive(false)
			plot.set_guided(false)
			if row != auto_row or _plot_by_id.is_empty():
				plot.set_watered(false, false)
	manual_row.visible = false
	auto_row.visible = not _plot_by_id.is_empty()
	manual_watering_can.visible = false
	magic_watering_can.visible = false
	bug_character.visible = false
	shu_shu.visible = false
	manual_path.visible = false
	variable_badge.visible = false
	spell_overlay.visible = false
	completion_card.visible = false
	growth_feedback.visible = false
	recovery_panel.visible = _recovery_active
	agent_status_panel.visible = true
	little_walnut.position = _little_walnut_rest_position
	task_card.position = _task_card_rest_position
	task_card.modulate.a = 1.0
	phase_strip.modulate.a = 1.0
	code_drawer.visible = false
	trace_label.text = _current_source
	_set_phase(Phase.READY if _authoritative_draft_loaded else Phase.CONNECTING)
	_update_authoritative_actions()


func _stop_activity() -> void:
	autosave_timer.stop()
	save_indicator_timer.stop()
	manual_watering_can.stop()
	magic_watering_can.stop()
	shu_shu.stop()
	_stop_watering_presentation()
	if _hud_intro_tween != null and _hud_intro_tween.is_valid():
		_hud_intro_tween.kill()
	if story_dialogue.visible:
		story_dialogue.skip_sequence()
	story_dialogue.visible = false


func _stop_watering_presentation() -> void:
	if _watering_tween != null and _watering_tween.is_valid():
		_watering_tween.kill()
	_watering_tween = null
	magic_watering_can.stop()
	magic_watering_can.visible = false
	variable_badge.visible = false


func _show_code_drawer() -> void:
	code_drawer.visible = true
	code_drawer.mouse_filter = Control.MOUSE_FILTER_STOP
	code_drawer.modulate.a = 1.0


func _play_hud_intro() -> void:
	task_card.position = _task_card_rest_position + Vector2(-14.0, 0.0)
	task_card.modulate.a = 0.0
	phase_strip.modulate.a = 0.0
	_hud_intro_tween = create_tween().set_parallel(true)
	_hud_intro_tween.tween_property(task_card, "position", _task_card_rest_position, _duration(0.28))
	_hud_intro_tween.tween_property(task_card, "modulate:a", 1.0, _duration(0.24))
	_hud_intro_tween.tween_property(phase_strip, "modulate:a", 1.0, _duration(0.22))


func _play_configured_story() -> void:
	pass


func _on_editor_text_changed() -> void:
	if _synchronizing_draft or not _authoritative_draft_loaded:
		return
	_current_source = editor.text
	drawer_save_state.visible = true
	drawer_save_state.text = "•"
	autosave_timer.start()


func _emit_draft_change() -> void:
	autosave_timer.stop()
	if _agent_mode and _authoritative_draft_loaded:
		_current_source = editor.text
		draft_changed.emit(_current_source)


func _on_submit_button_pressed() -> void:
	if not _agent_mode or not _authoritative_draft_loaded or _phase != Phase.READY:
		return
	_current_source = editor.text
	submit_requested.emit(_current_source)


func _on_hint_button_pressed() -> void:
	if not _agent_mode or not _authoritative_draft_loaded or _phase not in [Phase.READY, Phase.FAILED]:
		return
	hint_requested.emit(editor.text)


func _on_reset_button_pressed() -> void:
	if not _authoritative_draft_loaded:
		return
	_synchronizing_draft = true
	editor.text = _authoritative_source
	_synchronizing_draft = false
	_current_source = _authoritative_source
	draft_changed.emit(_current_source)


func _update_authoritative_actions(state_allows_actions: bool = true) -> void:
	var available := _agent_mode and _authoritative_draft_loaded and state_allows_actions
	submit_button.disabled = not available or _phase != Phase.READY
	hint_button.disabled = not available or _phase not in [Phase.READY, Phase.FAILED]
	reset_button.disabled = not available
	editor.editable = available


func _set_phase(value: Phase) -> void:
	_phase = value
	manual_phase_label.text = "✓" if value != Phase.CONNECTING else "○"
	loop_phase_label.text = "●" if value in [Phase.READY, Phase.FAILED] else ("✓" if value == Phase.COMPLETED else "○")
	run_phase_label.text = "●" if value == Phase.SUBMITTING else ("✓" if value == Phase.COMPLETED else "○")


func _valid_harvest_event(event: Dictionary) -> bool:
	if str(event.get("event_type", "")) != HARVEST_EVENT_TYPE or int(event.get("event_version", -1)) != HARVEST_EVENT_VERSION:
		return false
	var payload_value: Variant = event.get("payload")
	if not payload_value is Dictionary:
		return false
	var payload: Dictionary = payload_value
	var fields := ["actor_entity_id", "plot_id", "position", "crop_type", "growth_stage", "ready_to_harvest"]
	if payload.size() != fields.size():
		return false
	for field in fields:
		if not payload.has(field):
			return false
	var position_value: Variant = payload.position
	return (
		not str(payload.actor_entity_id).is_empty()
		and not str(payload.plot_id).is_empty()
		and position_value is Dictionary
		and (position_value as Dictionary).size() == 2
		and typeof((position_value as Dictionary).get("x")) == TYPE_INT
		and typeof((position_value as Dictionary).get("y")) == TYPE_INT
		and typeof(payload.crop_type) == TYPE_STRING
		and typeof(payload.growth_stage) == TYPE_INT
		and typeof(payload.ready_to_harvest) == TYPE_BOOL
		and bool(payload.ready_to_harvest)
	)


func _valid_snapshot_crop(crop_value: Variant) -> bool:
	if crop_value == null:
		return true
	if not crop_value is Dictionary:
		return false
	var crop: Dictionary = crop_value
	var fields := ["crop_type", "growth_stage", "planted_at_tick", "ready_to_harvest"]
	if crop.size() != fields.size():
		return false
	for field in fields:
		if not crop.has(field):
			return false
	return (
		typeof(crop.crop_type) == TYPE_STRING
		and not String(crop.crop_type).is_empty()
		and typeof(crop.growth_stage) == TYPE_INT
		and int(crop.growth_stage) >= 0
		and int(crop.growth_stage) <= 100
		and typeof(crop.planted_at_tick) == TYPE_INT
		and int(crop.planted_at_tick) >= 0
		and typeof(crop.ready_to_harvest) == TYPE_BOOL
	)


func _play_verified_watering(plot: WateringPlot, plot_id: String, speed: float, generation: int) -> void:
	if not _demo_active or generation != _run_generation:
		return
	magic_watering_can.visible = true
	magic_watering_can.global_position = plot.global_position + Vector3(-0.42, 1.70, 0.18)
	magic_watering_can.frame = 0
	magic_watering_can.speed_scale = speed / maxf(0.05, demo_timing_scale)
	variable_badge.visible = true
	variable_badge.text = plot_id
	variable_badge.global_position = plot.global_position + Vector3(0.0, 1.78, 0.0)
	magic_watering_can.play(&"pour")
	_watering_tween = create_tween()
	_watering_tween.tween_interval(_duration(HARVEST_DURATION_SECONDS / speed))
	await _watering_tween.finished
	if not _demo_active or generation != _run_generation or _presenting_plot_id != plot_id:
		return
	plot.set_watered(true, true)
	magic_watering_can.visible = false
	variable_badge.visible = false


func _reject_world_projection(message: String) -> bool:
	_stop_watering_presentation()
	_presenting_plot_id = ""
	_presenting_plot = null
	_plot_by_id.clear()
	_plot_position_by_id.clear()
	for child in auto_row.get_children():
		(child as WateringPlot).set_watered(false, false)
	auto_row.visible = false
	_show_recovery(message)
	return false


func _clear_authoritative_content() -> void:
	task_title.text = ""
	task_progress.text = ""
	_authoritative_story.clear()
	_authoritative_success_story = ""


func _show_agent_status(title: String, body: String) -> void:
	agent_status_panel.visible = true
	agent_status_title.text = title
	agent_status_body.text = body


func _show_recovery(message: String) -> void:
	_recovery_active = true
	recovery_message.text = message
	recovery_panel.visible = true


func _duration(seconds: float) -> float:
	return maxf(0.01, seconds * demo_timing_scale)
