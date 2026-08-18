extends Control

const AUTO_SAVE_DELAY_SECONDS := 0.8
const DRAWER_OPEN_DURATION := 0.30
const DRAWER_CLOSE_DURATION := 0.20
const TASK_TAG_COMPACT_HEIGHT := 82.0
const HUD_DIM_ALPHA := 0.42

signal build_action_finished(result: Dictionary)
signal activation_action_finished(result: Dictionary)
signal submit_action_finished(result: Dictionary)
signal patch_request_action_finished(result: Dictionary)
signal patch_decision_action_finished(decision: String, result: Dictionary)

## PatchDecision is outside INT1. The formal AppRoot leaves this false, so the
## optional review dialog and all decision signal wiring are absent.
@export var patch_decisions_enabled := false
## AppRoot now presents GameFlow as the student-facing UI. In that composition
## this legacy workspace remains only as the authoritative World renderer and
## must not connect duplicate controls or seed a replacement Draft.
@export var interactive_enabled := true

@onready var task_tag: Control = $Hud/SafeArea/EdgeLayer/TaskTag
@onready var task_title: Label = $Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskTitle
@onready var task_goal: Label = $Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskGoal
@onready var task_progress: Label = $Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskProgressPanel
@onready var code_drawer: Control = $DrawerLayer/CodeDrawer
@onready var drawer_content: Control = $DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content
@onready var code_drawer_button: Button = $Hud/SafeArea/EdgeLayer/ToolRail/CodeDrawerButton
@onready var hint_button: Button = $Hud/SafeArea/EdgeLayer/ToolRail/HintButton
@onready var request_ai_patch_button: Button = $Hud/SafeArea/EdgeLayer/ToolRail/RequestAiPatchButton
@onready var playback_speed_button: Button = $Hud/SafeArea/EdgeLayer/ToolRail/PlaybackSpeedButton
@onready var skip_playback_button: Button = $Hud/SafeArea/EdgeLayer/ToolRail/SkipPlaybackButton
@onready var replay_playback_button: Button = $Hud/SafeArea/EdgeLayer/ToolRail/ReplayPlaybackButton
@onready var tool_rail: Control = $Hud/SafeArea/EdgeLayer/ToolRail
@onready var auto_save_pill: Control = $Hud/SafeArea/EdgeLayer/AutoSavePill
@onready var auto_save_state: Label = $Hud/SafeArea/EdgeLayer/AutoSavePill/AutoSaveState
@onready var reset_button: Button = $DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/RunControlPanel/Actions/ResetButton
@onready var build_button: Button = $DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/RunControlPanel/Actions/BuildButton
@onready var activation_button: Button = $DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/RunControlPanel/Actions/ActivationButton
@onready var submit_button: Button = $DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/RunControlPanel/Actions/SubmitButton
@onready var editor: CodeEdit = $DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeEditorPanel/Content/CodeEditor
@onready var editor_save_state: Label = $DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeEditorPanel/Content/Header/SaveState
@onready var dialogue: Node = $DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DialoguePanel
@onready var agent_presenter: Node = $AgentInteractionPresenter
@onready var world_viewport: Node = $WorldViewport
@onready var result_panel: Node = $Hud/SafeArea/EdgeLayer/Toast
@onready var result_text: RichTextLabel = $Hud/SafeArea/EdgeLayer/Toast/ResultText
@onready var reset_dialog: ConfirmationDialog = $ResetCodeDialog
@onready var growth_summary: AcceptDialog = $GrowthSummaryPanel
@onready var auto_save_timer: Timer = $AutoSaveTimer
@onready var toast_timer: Timer = $ToastTimer
@onready var store: WalnutClientStore = get_node_or_null("/root/ClientStore") as WalnutClientStore
@onready var session: Node = get_node_or_null("/root/SessionController")

var pending_patch_interaction: Dictionary = {}
var patch_dialog: ConfirmationDialog
var reject_patch_button: Button
var _synchronizing_editor := false
var _drawer_tween: Tween
var _drawer_content_tween: Tween
var _task_tag_tween: Tween
var _hud_focus_tween: Tween
var _toast_tween: Tween
var _button_tweens: Dictionary = {}
var _toast_rest_position := Vector2.ZERO


func _ready() -> void:
	if not interactive_enabled:
		visible = false
		# CanvasLayer rendering/input is independent of a Control parent's
		# visibility, so every legacy UI layer must be disabled explicitly.
		for layer_path: String in ["AgentInteractionPresenter", "Hud", "DrawerLayer"]:
			var layer := get_node_or_null(layer_path) as CanvasLayer
			if layer != null:
				layer.visible = false
		return
	if store == null or session == null:
		push_error("TaskWorkspace requires ClientStore and SessionController autoloads.")
		return
	store.workspace_changed.connect(_on_workspace_changed)
	store.content_changed.connect(_on_content_changed)
	store.draft_changed.connect(_on_draft_changed)
	store.objective_result_changed.connect(_on_objective_result_changed)
	store.error_reported.connect(_on_error_reported)
	store.flow_changed.connect(_on_flow_changed)
	session.capability_unavailable.connect(_on_capability_unavailable)
	session.build_resolved.connect(_on_build_resolved)
	session.run_resolved.connect(_on_run_resolved)
	session.interactions_recovered.connect(_on_interactions_recovered)
	agent_presenter.world_cue_requested.connect(_on_agent_world_cue_requested)
	if session.has_signal("world_playback_state_changed"):
		session.world_playback_state_changed.connect(_on_world_playback_state_changed)
	if session.has_signal("patch_decision_resolved"):
		session.patch_decision_resolved.connect(_on_patch_decision_resolved)
	if patch_decisions_enabled:
		_install_patch_decision_ui()
	session.interactions_recovered.connect(_on_patch_interactions_recovered)
	reset_dialog.confirmed.connect(reset_code_to_starter)
	editor.text_changed.connect(_on_editor_text_changed)
	auto_save_timer.timeout.connect(_on_auto_save_timeout)
	toast_timer.timeout.connect(_hide_toast)
	$Hud/SafeArea/EdgeLayer/TaskTag/TaskButton.pressed.connect(toggle_task_tag)
	code_drawer_button.pressed.connect(toggle_code_drawer)
	$DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DrawerHeader/CloseButton.pressed.connect(hide_code_drawer)
	hint_button.pressed.connect(_on_hint_requested)
	request_ai_patch_button.pressed.connect(_on_ai_patch_requested)
	playback_speed_button.pressed.connect(_on_playback_speed_requested)
	skip_playback_button.pressed.connect(_on_skip_playback_requested)
	replay_playback_button.pressed.connect(_on_replay_playback_requested)
	reset_button.pressed.connect(_on_reset_requested)
	build_button.pressed.connect(_on_build_requested)
	activation_button.pressed.connect(_on_activation_requested)
	submit_button.pressed.connect(_on_submit_requested)
	code_drawer.resized.connect(_place_drawer_offscreen_if_hidden)
	_toast_rest_position = result_panel.position
	for button: Button in [code_drawer_button, hint_button, request_ai_patch_button, playback_speed_button, skip_playback_button, replay_playback_button, reset_button, build_button, activation_button, submit_button, $DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DrawerHeader/CloseButton]:
		button.button_down.connect(_animate_button_press.bind(button))
		button.button_up.connect(_animate_button_release.bind(button))
	call_deferred("_prepare_hud")
	if not store.workspace.is_empty():
		_on_workspace_changed(store.workspace.duplicate(true))
	if not store.content.is_empty():
		_on_content_changed(store.content.duplicate(true))
	if store.local_source.is_empty():
		_set_editor_text("#include <iostream>\n\nvoid run() {\n    // 让小核桃从这里开始工作\n}\n")
		store.mark_draft_dirty(editor.text)
	else:
		_set_editor_text(store.local_source)
	_refresh_action_buttons()


func _install_patch_decision_ui() -> void:
	if patch_dialog != null:
		return
	patch_dialog = ConfirmationDialog.new()
	patch_dialog.name = "CodePatchDialog"
	patch_dialog.title = "AI code change"
	patch_dialog.dialog_text = "Review the complete proposed change before deciding."
	add_child(patch_dialog)
	patch_dialog.confirmed.connect(_accept_patch)
	# Escape/window-close/cancel only dismisses the preview. A Patch REJECT is a
	# distinct student decision and therefore has its own explicit button.
	patch_dialog.canceled.connect(_close_patch_preview)
	patch_dialog.custom_action.connect(_on_patch_dialog_custom_action)
	reject_patch_button = patch_dialog.add_button("拒绝修改", true, "reject_patch")


func configure_skill_patch_enabled(enabled: bool) -> void:
	patch_decisions_enabled = enabled
	pending_patch_interaction.clear()
	if enabled:
		_install_patch_decision_ui()
	else:
		if patch_dialog != null:
			remove_child(patch_dialog)
			patch_dialog.free()
			patch_dialog = null
			reject_patch_button = null
	request_ai_patch_button.visible = false
	request_ai_patch_button.disabled = true
	_refresh_action_buttons()


func _prepare_hud() -> void:
	_place_drawer_offscreen_if_hidden()
	task_progress.visible = true
	task_goal.visible = false
	task_tag.size.y = TASK_TAG_COMPACT_HEIGHT
	task_tag.modulate.a = 0.0
	task_tag.position.x -= 18.0
	tool_rail.modulate.a = 0.0
	var intro := create_tween().set_parallel(true)
	intro.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	intro.tween_property(task_tag, "modulate:a", 1.0, 0.30)
	intro.tween_property(task_tag, "position:x", task_tag.position.x + 18.0, 0.30)
	intro.tween_property(tool_rail, "modulate:a", 1.0, 0.24).set_delay(0.05)


func _place_drawer_offscreen_if_hidden() -> void:
	if not code_drawer.visible:
		code_drawer.position.x = code_drawer.size.x
		code_drawer.mouse_filter = Control.MOUSE_FILTER_IGNORE


func toggle_task_tag() -> void:
	if _task_tag_tween != null:
		_task_tag_tween.kill()
	task_tag.pivot_offset = task_tag.size * 0.5
	_task_tag_tween = create_tween()
	_task_tag_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_task_tag_tween.tween_property(task_tag, "scale", Vector2(0.97, 0.97), 0.06)
	_task_tag_tween.tween_property(task_tag, "scale", Vector2.ONE, 0.14)


func toggle_code_drawer() -> void:
	if code_drawer.visible:
		hide_code_drawer()
	else:
		show_code_drawer()


func show_code_drawer() -> void:
	if _drawer_tween != null:
		_drawer_tween.kill()
	if _drawer_content_tween != null:
		_drawer_content_tween.kill()
	code_drawer.visible = true
	code_drawer.mouse_filter = Control.MOUSE_FILTER_STOP
	code_drawer.position.x = code_drawer.size.x
	code_drawer.modulate.a = 0.0
	drawer_content.modulate.a = 0.0
	_drawer_tween = create_tween().set_parallel(true)
	_drawer_tween.set_trans(Tween.TRANS_QUINT).set_ease(Tween.EASE_OUT)
	_drawer_tween.tween_property(code_drawer, "position:x", 0.0, DRAWER_OPEN_DURATION)
	_drawer_tween.tween_property(code_drawer, "modulate:a", 1.0, DRAWER_OPEN_DURATION)
	_drawer_content_tween = create_tween().set_parallel(true)
	_drawer_content_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_drawer_content_tween.tween_property(drawer_content, "modulate:a", 1.0, 0.16).set_delay(0.08)
	_set_world_hud_weight(HUD_DIM_ALPHA)


func hide_code_drawer() -> void:
	if not code_drawer.visible:
		return
	if _drawer_tween != null:
		_drawer_tween.kill()
	if _drawer_content_tween != null:
		_drawer_content_tween.kill()
	code_drawer.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_drawer_tween = create_tween().set_parallel(true)
	_drawer_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_IN)
	_drawer_tween.tween_property(code_drawer, "position:x", code_drawer.size.x, DRAWER_CLOSE_DURATION)
	_drawer_tween.tween_property(code_drawer, "modulate:a", 0.0, DRAWER_CLOSE_DURATION)
	_drawer_tween.chain().tween_callback(func() -> void:
		code_drawer.visible = false
		code_drawer.position.x = code_drawer.size.x
	)
	_set_world_hud_weight(1.0)


func _set_world_hud_weight(target_alpha: float) -> void:
	if _hud_focus_tween != null and _hud_focus_tween.is_valid():
		_hud_focus_tween.kill()
	_hud_focus_tween = create_tween().set_parallel(true)
	_hud_focus_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_hud_focus_tween.tween_property(task_tag, "modulate:a", target_alpha, 0.18)
	_hud_focus_tween.tween_property(tool_rail, "modulate:a", maxf(target_alpha, 0.35), 0.18)


func _animate_button_press(button: Button) -> void:
	button.pivot_offset = button.size * 0.5
	var tween := _replace_button_tween(button)
	tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	tween.tween_property(button, "scale", Vector2(0.94, 0.94), 0.08)


func _animate_button_release(button: Button) -> void:
	var tween := _replace_button_tween(button)
	tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	tween.tween_property(button, "scale", Vector2.ONE, 0.12)


func _replace_button_tween(button: Button) -> Tween:
	var active: Variant = _button_tweens.get(button)
	if active is Tween and active.is_valid():
		active.kill()
	var tween := create_tween()
	_button_tweens[button] = tween
	return tween


func _on_editor_text_changed() -> void:
	if _synchronizing_editor:
		return
	store.mark_draft_dirty(editor.text)
	if not store.draft.is_empty():
		auto_save_timer.start(AUTO_SAVE_DELAY_SECONDS)


func _on_workspace_changed(workspace: Dictionary) -> void:
	task_progress.text = _workspace_progress_text(workspace)


func _on_content_changed(content: Dictionary) -> void:
	var task: Variant = content.get("task")
	if not task is Dictionary:
		return
	task_title.text = str(task.get("name", task_title.text))
	task_goal.text = str(task.get("goal", task_goal.text))
	$Hud/SafeArea/EdgeLayer/TaskTag/TaskButton.tooltip_text = task_goal.text
	reset_button.disabled = _starter_source_from_content().is_empty()


func _on_draft_changed(source: String, state: int) -> void:
	if editor.text != source:
		_set_editor_text(source)
	var label := "✓ 已自动保存"
	match state:
		WalnutClientStore.DraftState.CLEAN:
			auto_save_pill.visible = false
		WalnutClientStore.DraftState.DIRTY:
			label = "• 等待自动保存"
			auto_save_pill.visible = false
		WalnutClientStore.DraftState.SAVING:
			label = "↻ 正在自动保存"
			auto_save_pill.visible = true
		WalnutClientStore.DraftState.CONFLICT:
			label = "! 保存冲突"
			auto_save_pill.visible = true
		WalnutClientStore.DraftState.SAVE_FAILED:
			label = "! 自动保存失败"
			auto_save_pill.visible = true
	auto_save_state.text = label
	editor_save_state.text = label
	_refresh_action_buttons()


func _on_flow_changed(_flow: int) -> void:
	_refresh_action_buttons()


func _on_world_playback_state_changed(_state: String) -> void:
	_refresh_action_buttons()


func _refresh_action_buttons() -> void:
	_refresh_playback_buttons()
	_refresh_patch_button()
	var busy := store.flow_state in [WalnutClientStore.FlowState.BUILDING, WalnutClientStore.FlowState.ACTIVATING, WalnutClientStore.FlowState.TURN_RUNNING, WalnutClientStore.FlowState.PLAYING]
	var authority_ready := store.flow_state in [
		WalnutClientStore.FlowState.READY,
		WalnutClientStore.FlowState.BUILD_FAILED,
		WalnutClientStore.FlowState.CERTIFIED,
		WalnutClientStore.FlowState.ACTIVE,
		WalnutClientStore.FlowState.COMPLETED,
	]
	var controller_active: Variant = session.get("active_skill_tuple")
	var runnable_active: Dictionary = (
		controller_active
		if controller_active is Dictionary
		else store.active_skill_tuple
	)
	build_button.disabled = busy or not authority_ready or store.draft.is_empty()
	activation_button.disabled = busy or store.flow_state != WalnutClientStore.FlowState.CERTIFIED
	submit_button.disabled = busy or not authority_ready or runnable_active.is_empty()
	build_button.text = "正在构建…" if store.flow_state == WalnutClientStore.FlowState.BUILDING else "① 构建"
	activation_button.text = "正在激活…" if store.flow_state == WalnutClientStore.FlowState.ACTIVATING else "② 激活"
	submit_button.text = _submit_label_for_flow(store.flow_state) if store.flow_state in [WalnutClientStore.FlowState.TURN_RUNNING, WalnutClientStore.FlowState.PLAYING] else "③ 提交并运行"


func _refresh_playback_buttons() -> void:
	var presentation_available: bool = (
		session.has_method("can_replay_world_result")
		and session.get("world_presentation_enabled") == true
	)
	playback_speed_button.visible = presentation_available
	skip_playback_button.visible = presentation_available
	replay_playback_button.visible = presentation_available
	playback_speed_button.disabled = not presentation_available
	skip_playback_button.disabled = not presentation_available or store.flow_state != WalnutClientStore.FlowState.PLAYING
	replay_playback_button.disabled = (
		not presentation_available
		or store.flow_state != WalnutClientStore.FlowState.COMPLETED
		or not session.has_method("can_replay_world_result")
		or session.call("can_replay_world_result") != true
	)


func _refresh_patch_button() -> void:
	var available: bool = (
		patch_decisions_enabled
		and (
			not pending_patch_interaction.is_empty()
			or session.has_method("can_request_ai_patch")
			and session.call("can_request_ai_patch") == true
		)
	)
	request_ai_patch_button.visible = available
	request_ai_patch_button.text = "查看 AI 修改" if not pending_patch_interaction.is_empty() else "请求 AI 帮助修改"
	request_ai_patch_button.disabled = (
		not available
		or store.flow_state in [
			WalnutClientStore.FlowState.BUILDING,
			WalnutClientStore.FlowState.ACTIVATING,
			WalnutClientStore.FlowState.TURN_RUNNING,
			WalnutClientStore.FlowState.PLAYING,
		]
	)


func _on_playback_speed_requested() -> void:
	if not session.has_method("set_world_playback_speed"):
		return
	var next_speed := 2.0 if playback_speed_button.text == "1x" else 1.0
	if session.call("set_world_playback_speed", next_speed) == true:
		playback_speed_button.text = "2x" if next_speed == 2.0 else "1x"


func _on_skip_playback_requested() -> void:
	if session.has_method("skip_world_playback"):
		session.call("skip_world_playback")


func _on_replay_playback_requested() -> void:
	if not session.has_method("replay_world_result"):
		return
	var replay: Dictionary = await session.call("replay_world_result")
	if not replay.get("ok", false):
		show_toast(str(replay.get("error", {}).get("message", "Authoritative replay failed.")), true)


func _set_editor_text(source: String) -> void:
	_synchronizing_editor = true
	editor.text = source
	_synchronizing_editor = false


func _on_auto_save_timeout() -> void:
	if store.draft.is_empty() or store.draft_state != WalnutClientStore.DraftState.DIRTY:
		return
	await session.request_save()
	if store.draft_state == WalnutClientStore.DraftState.DIRTY:
		auto_save_timer.start(AUTO_SAVE_DELAY_SECONDS)


func _on_reset_requested() -> void:
	if _starter_source_from_content().is_empty():
		show_toast("当前任务没有可恢复的初始代码。", true)
		return
	reset_dialog.popup_centered()


func reset_code_to_starter() -> void:
	var source := _starter_source_from_content()
	if source.is_empty():
		show_toast("当前任务没有可恢复的初始代码。", true)
		return
	_set_editor_text(source)
	store.mark_draft_dirty(source)
	auto_save_timer.start(AUTO_SAVE_DELAY_SECONDS)
	show_toast("已恢复初始代码，正在自动保存。")


func _starter_source_from_content() -> String:
	var task: Variant = store.content.get("task")
	if not task is Dictionary:
		return ""
	var starter: Variant = task.get("starter_skill")
	if not starter is Dictionary:
		return ""
	var bundle: Variant = starter.get("source_bundle")
	if not bundle is Dictionary:
		return ""
	var entrypoint := str(bundle.get("entrypoint", ""))
	var files: Variant = bundle.get("files")
	if not files is Array:
		return ""
	for file_value: Variant in files:
		if file_value is Dictionary and str(file_value.get("path", "")) == entrypoint:
			return str(file_value.get("content", ""))
	return ""


func _on_build_requested() -> Dictionary:
	auto_save_timer.stop()
	if not session.has_method("request_build"):
		var unavailable := {"ok": false, "stage": "BUILD", "message": "构建链路正在准备中。"}
		show_toast(unavailable.message, true)
		build_action_finished.emit(unavailable)
		return unavailable
	await session.request_build()
	var result := {
		"ok": store.flow_state == WalnutClientStore.FlowState.CERTIFIED,
		"stage": "BUILD",
		"message": "Build reached CERTIFIED." if store.flow_state == WalnutClientStore.FlowState.CERTIFIED else "Build did not reach a certified terminal resource.",
	}
	if not result.ok:
		show_toast(str(result.message), true)
	build_action_finished.emit(result.duplicate(true))
	return result


func _on_activation_requested() -> Dictionary:
	if not session.has_method("request_activation"):
		var unavailable := {"ok": false, "stage": "ACTIVATE", "message": "激活链路正在准备中。"}
		show_toast(unavailable.message, true)
		activation_action_finished.emit(unavailable)
		return unavailable
	await session.request_activation()
	var result := {
		"ok": store.flow_state == WalnutClientStore.FlowState.ACTIVE and not store.active_skill_tuple.is_empty(),
		"stage": "ACTIVATE",
		"message": "The exact certified Skill tuple is active." if store.flow_state == WalnutClientStore.FlowState.ACTIVE else "Activation did not publish the exact certified Skill tuple.",
	}
	if not result.ok:
		show_toast(str(result.message), true)
	activation_action_finished.emit(result.duplicate(true))
	return result


func _on_submit_requested() -> Dictionary:
	auto_save_timer.stop()
	if not session.has_method("request_submit_and_run"):
		show_toast("提交链路正在准备中。", true)
		var unavailable := {"ok": false, "stage": "RUN", "message": "提交链路正在准备中。"}
		submit_action_finished.emit(unavailable)
		return unavailable
	var result: Dictionary = await session.request_submit_and_run()
	if not bool(result.get("ok", false)):
		show_toast(str(result.get("message", "提交未完成，请查看结果。")), true)
	submit_action_finished.emit(result.duplicate(true))
	return result


func _on_hint_requested() -> void:
	show_code_drawer()
	show_toast("正在请求教学提示。")
	await session.request_hint()


func _on_ai_patch_requested() -> Dictionary:
	if not patch_decisions_enabled or not session.has_method("request_ai_patch"):
		var unavailable := {
			"ok": false,
			"error": {
				"code": "SKILL_PATCH_REQUEST_UNAVAILABLE",
				"message": "Skill Patch request is not enabled by both rollout authorities.",
			},
		}
		patch_request_action_finished.emit(unavailable.duplicate(true))
		return unavailable
	if not pending_patch_interaction.is_empty() and patch_dialog != null:
		patch_dialog.popup_centered()
		var reopened := {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {
				"outcome": "SKILL_PATCH_PREVIEW_REOPENED",
				"interaction": pending_patch_interaction.duplicate(true),
			},
		}
		patch_request_action_finished.emit(reopened.duplicate(true))
		return reopened
	request_ai_patch_button.disabled = true
	show_toast("正在基于当前可见失败请求 AI 修改建议；代码尚未改变。")
	var result: Dictionary = await session.request_ai_patch()
	if not result.get("ok", false):
		show_toast(_result_error_message(result, "AI Patch request did not complete."), true)
	_refresh_action_buttons()
	patch_request_action_finished.emit(result.duplicate(true))
	return result


func _on_objective_result_changed(result: Dictionary) -> void:
	show_toast(str(result.get("summary", "等待运行结果。")))


func _on_error_reported(error: Dictionary) -> void:
	show_toast(str(error.get("message", "发生未分类错误。")), true)


func _on_capability_unavailable(_capability: String, message: String) -> void:
	show_toast(message, true)


func _on_build_resolved(build: Dictionary) -> void:
	result_panel.call("show_build", build)
	_reveal_result_panel()


func _on_run_resolved(run: Dictionary) -> void:
	result_panel.call("show_run", run)
	_reveal_result_panel()


func show_toast(message: String, is_error: bool = false) -> void:
	result_text.text = "[color=%s]%s[/color]" % ["#ffd6c5" if is_error else "#f7f0d3", message.replace("[", "[lb]")]
	_reveal_result_panel()


func _reveal_result_panel() -> void:
	if _toast_tween != null:
		_toast_tween.kill()
	toast_timer.stop()
	result_panel.visible = true
	result_panel.modulate.a = 0.0
	result_panel.position = _toast_rest_position + Vector2(0.0, 10.0)
	_toast_tween = create_tween().set_parallel(true)
	_toast_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_toast_tween.tween_property(result_panel, "modulate:a", 1.0, 0.18)
	_toast_tween.tween_property(result_panel, "position", _toast_rest_position, 0.18)
	_toast_tween.chain().tween_callback(func() -> void: toast_timer.start())


func _hide_toast() -> void:
	if not result_panel.visible:
		return
	if _toast_tween != null:
		_toast_tween.kill()
	_toast_tween = create_tween().set_parallel(true)
	_toast_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_IN)
	_toast_tween.tween_property(result_panel, "modulate:a", 0.0, 0.20)
	_toast_tween.tween_property(result_panel, "position:y", _toast_rest_position.y + 8.0, 0.20)
	_toast_tween.chain().tween_callback(func() -> void:
		result_panel.visible = false
		result_panel.position = _toast_rest_position
	)


func _on_patch_interactions_recovered(interactions: Array[Dictionary]) -> void:
	if not patch_decisions_enabled or patch_dialog == null or interactions.is_empty():
		return
	if not session.has_method("validate_minimal_skill_patch_interaction"):
		return
	if not pending_patch_interaction.is_empty():
		var pending_patch: Variant = pending_patch_interaction.get("skill_patch")
		for interaction: Dictionary in interactions:
			var interaction_patch: Variant = interaction.get("skill_patch")
			if (
				pending_patch is Dictionary
				and interaction_patch is Dictionary
				and str(interaction.get("interaction_id", "")) == str(pending_patch_interaction.get("interaction_id", ""))
				and str(interaction_patch.get("patch_id", "")) == str(pending_patch.get("patch_id", ""))
				and interaction.get("patch_decision") is Dictionary
			):
				_clear_resolved_patch_proposal()
				break
	var latest: Dictionary = {}
	for index in range(interactions.size() - 1, -1, -1):
		var candidate: Dictionary = interactions[index]
		var proposal_validation: Dictionary = session.call("validate_minimal_skill_patch_interaction", candidate)
		if proposal_validation.get("ok", false):
			latest = candidate
			break
	if latest.is_empty():
		return
	var patch: Variant = latest.get("skill_patch")
	if not patch is Dictionary or not bool(patch.get("requires_student_confirmation", false)):
		return
	pending_patch_interaction = latest.duplicate(true)
	patch_dialog.dialog_text = _format_patch_review(patch)
	patch_dialog.get_ok_button().text = "接受修改"
	patch_dialog.get_cancel_button().text = "关闭预览"
	patch_dialog.popup_centered()
	_refresh_patch_button()


func _close_patch_preview() -> void:
	# Deliberately preserve pending_patch_interaction. Closing the preview is not
	# an ACCEPT or REJECT and the same exact proposal remains reopenable.
	_refresh_patch_button()


func _on_patch_dialog_custom_action(action: StringName) -> void:
	if action == &"reject_patch":
		await _reject_patch()


func _accept_patch() -> Dictionary:
	if not patch_decisions_enabled:
		pending_patch_interaction.clear()
		var excluded := {
			"ok": false,
			"error": {"code": "PATCH_DECISION_EXCLUDED", "message": "Skill Patch capability is disabled."},
		}
		patch_decision_action_finished.emit("ACCEPT", excluded.duplicate(true))
		return excluded
	if pending_patch_interaction.is_empty():
		var missing := {
			"ok": false,
			"error": {"code": "PATCH_PROPOSAL_MISSING", "message": "No reviewed Skill Patch proposal is pending."},
		}
		patch_decision_action_finished.emit("ACCEPT", missing.duplicate(true))
		return missing
	var result: Dictionary = await session.decide_patch(pending_patch_interaction, "ACCEPT")
	show_toast(
		"已加载后端确认的 Draft。"
		if result.get("ok", false)
		else _result_error_message(result, "Code change was not applied."),
		not result.get("ok", false),
	)
	if result.get("ok", false):
		pending_patch_interaction.clear()
	else:
		patch_dialog.popup_centered()
	_refresh_patch_button()
	patch_decision_action_finished.emit("ACCEPT", result.duplicate(true))
	return result


func _reject_patch() -> Dictionary:
	if not patch_decisions_enabled:
		pending_patch_interaction.clear()
		var excluded := {
			"ok": false,
			"error": {"code": "PATCH_DECISION_EXCLUDED", "message": "Skill Patch capability is disabled."},
		}
		patch_decision_action_finished.emit("REJECT", excluded.duplicate(true))
		return excluded
	if pending_patch_interaction.is_empty():
		var missing := {
			"ok": false,
			"error": {"code": "PATCH_PROPOSAL_MISSING", "message": "No reviewed Skill Patch proposal is pending."},
		}
		patch_decision_action_finished.emit("REJECT", missing.duplicate(true))
		return missing
	var result: Dictionary = await session.decide_patch(pending_patch_interaction, "REJECT")
	show_toast(
		"已拒绝修改，本地代码保持不变。"
		if result.get("ok", false)
		else _result_error_message(result, "Reject decision did not complete."),
		not result.get("ok", false),
	)
	if result.get("ok", false):
		pending_patch_interaction.clear()
	else:
		patch_dialog.popup_centered()
	_refresh_patch_button()
	patch_decision_action_finished.emit("REJECT", result.duplicate(true))
	return result


func _format_patch_review(patch: Dictionary) -> String:
	var lines: Array[String] = [
		"AI CODE PATCH (NOT APPLIED)",
		"Reason: %s" % str(patch.get("rationale", "")),
		"Base Draft: revision %s · %s" % [patch.get("base_draft_revision", "?"), patch.get("base_draft_sha256", "")],
		"Result Draft hash: %s" % str(patch.get("result_draft_sha256", "")),
		"", "Exact file changes:",
	]
	for operation_value: Variant in patch.get("operations", []):
		if not operation_value is Dictionary:
			continue
		var operation: Dictionary = operation_value
		var path := str(operation.get("path", ""))
		lines.append("[%s] %s" % [str(operation.get("operation", "UNKNOWN")), path])
		lines.append("Previous content SHA-256: %s" % str(operation.get("previous_content_sha256", "")))
		lines.append("New content SHA-256: %s" % str(operation.get("content_sha256", "")))
		lines.append("--- BEFORE %s" % path)
		lines.append(_canonical_draft_file_content(path))
		lines.append("+++ AFTER %s" % path)
		lines.append(str(operation.get("content", "")))
	lines.append("")
	lines.append("Failed-run Evidence:")
	for reference_value: Variant in patch.get("evidence_refs", []):
		if not reference_value is Dictionary:
			continue
		var reference: Dictionary = reference_value
		lines.append("- %s · %s · sha256=%s · uri=%s" % [
			reference.get("evidence_id", ""), reference.get("evidence_type", ""),
			reference.get("sha256", ""), reference.get("uri", ""),
		])
	return "\n".join(lines)


func _canonical_draft_file_content(path: String) -> String:
	var bundle: Variant = store.draft.get("source_bundle")
	if not bundle is Dictionary or not bundle.get("files") is Array:
		return "<canonical source unavailable>"
	for file_value: Variant in bundle.files:
		if file_value is Dictionary and str(file_value.get("path", "")) == path:
			return str(file_value.get("content", ""))
	return "<canonical source unavailable>"


func _on_interactions_recovered(interactions: Array[Dictionary]) -> void:
	if interactions.is_empty():
		return
	for index in range(interactions.size() - 1, -1, -1):
		var interaction: Dictionary = interactions[index]
		if str(interaction.get("response_type", "")) == "growth_summary":
			_show_growth_summary(interaction)
			break
	var latest: Dictionary = interactions.back()
	if session.has_method("register_visible_patch_failure"):
		var registered := false
		var recovery_status: Dictionary = {}
		for index in range(interactions.size() - 1, -1, -1):
			registered = bool(session.call("register_visible_patch_failure", interactions[index]))
			if registered:
				break
			if session.has_method("patch_failure_recovery_result"):
				var candidate_status: Dictionary = session.call("patch_failure_recovery_result")
				if not candidate_status.is_empty():
					recovery_status = candidate_status
					break
		if not registered and not recovery_status.is_empty():
			show_toast(_result_error_message(recovery_status, "Recovered Patch failure authority is not proven."), true)
		_refresh_patch_button()
	var role_id := StringName(str(latest.get("role", "")))
	var question: Variant = latest.get("question")
	var hint_level_value: Variant = latest.get("hint_level")
	var hint_level := hint_level_value as int if typeof(hint_level_value) == TYPE_INT else 0
	dialogue.show_interaction(agent_presenter.display_name_for(role_id), str(latest.get("feedback", {}).get("message", "")), "" if question == null else str(question), str(latest.get("response_type", "message")), hint_level)
	for interaction in interactions:
		if not str(interaction.get("interaction_id", "")).is_empty():
			agent_presenter.enqueue_interaction(interaction)


func _on_agent_world_cue_requested(presentation_key: StringName, active: bool) -> void:
	if world_viewport.has_method("present_agent_world_cue"):
		world_viewport.present_agent_world_cue(presentation_key, active)


func _on_patch_decision_resolved(interaction_id: String, patch_id: String, _decision: String) -> void:
	var patch: Variant = pending_patch_interaction.get("skill_patch")
	if (
		patch is Dictionary
		and str(pending_patch_interaction.get("interaction_id", "")) == interaction_id
		and str(patch.get("patch_id", "")) == patch_id
	):
		_clear_resolved_patch_proposal()


func _clear_resolved_patch_proposal() -> void:
	pending_patch_interaction.clear()
	if patch_dialog != null:
		patch_dialog.hide()
	_refresh_patch_button()


func _result_error_message(result: Dictionary, fallback: String) -> String:
	var error: Variant = result.get("error")
	if not error is Dictionary:
		return fallback
	var code := str(error.get("code", "UNKNOWN_ERROR"))
	var message := str(error.get("message", fallback))
	return "[%s] %s" % [code, message]


func _workspace_progress_text(workspace: Dictionary) -> String:
	var current_task: Variant = workspace.get("current_task")
	if not current_task is Dictionary:
		return "• 恢复中"
	var status := str(current_task.get("status", "NOT_STARTED"))
	return str({"NOT_STARTED": "○ 未开始", "IN_PROGRESS": "● 进行中", "COMPLETED": "✓ 已完成", "ABANDONED": "– 已中止"}.get(status, "? 未知"))


func _submit_label_for_flow(flow: int) -> String:
	match flow:
		WalnutClientStore.FlowState.BUILDING: return "正在编译…"
		WalnutClientStore.FlowState.ACTIVATING: return "正在激活…"
		WalnutClientStore.FlowState.TURN_RUNNING: return "正在运行…"
		WalnutClientStore.FlowState.PLAYING: return "正在播放动画…"
	return "提交并运行"


func _show_growth_summary(interaction: Dictionary) -> void:
	var feedback: Dictionary = interaction.get("feedback", {})
	growth_summary.dialog_text = str(feedback.get("message", ""))
	growth_summary.popup_centered()
