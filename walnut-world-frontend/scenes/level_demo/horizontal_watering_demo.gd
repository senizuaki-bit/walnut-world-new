extends Node3D

enum Phase {
	MANUAL_WATERING,
	DISCOVER_REPEAT,
	CODE_CHALLENGE,
	RUNNING,
	COMPLETED,
}

const STARTER_CODE := "for (int i = __; i < __; i++) {\n    cout << \"浇\" << __ << \"号土地中……\\n\";\n}\ncout << \"浇灌完成！\";"
const CORRECT_CODE := "for (int i = 0; i < 5; i++) {\n    cout << \"浇\" << i << \"号土地中……\\n\";\n}\ncout << \"浇灌完成！\";"
const INTRO_LINES: Array[String] = [
	"我刚刚种了一排幼苗",
	"它们现在非常需要浇水~",
	"小朋友, 你能点击土地让小核桃帮我浇水吗?",
]
const DING_DANG_LINES: Array[String] = [
	"孩子，你做得真不错",
	"这里还有五块地需要浇水",
	"让叮当师傅来教你一个简单的方法吧",
]
const YAYA_PORTRAIT: Texture2D = preload("res://assets/art/generated/characters/yaya_sprout.png")
const DING_DANG_PORTRAIT: Texture2D = preload("res://assets/art/generated/characters/master_ding_dang.png")

@export_range(0.0, 2.0, 0.05) var demo_timing_scale: float = 1.0

@onready var manual_row: Node3D = $ManualRow
@onready var auto_row: Node3D = $AutoRow
@onready var little_walnut: Sprite3D = $Cast/LittleWalnut
@onready var bug_character: Sprite3D = $Cast/Bug
@onready var shu_shu: AnimatedSprite3D = $Cast/ShuShu
@onready var manual_watering_can: AnimatedSprite3D = $ManualWateringCan
@onready var magic_watering_can: AnimatedSprite3D = $MagicWateringCan
@onready var manual_path: Label3D = $Guidance/ManualPath
@onready var variable_badge: Label3D = $Guidance/VariableBadge
@onready var task_card: Control = $Hud/SafeArea/TaskCard
@onready var phase_strip: Control = $Hud/SafeArea/PhaseStrip
@onready var task_progress: Label = $Hud/SafeArea/TaskCard/Margin/Content/TaskProgress
@onready var manual_phase_label: Label = $Hud/SafeArea/PhaseStrip/Margin/Phases/ManualPhase
@onready var loop_phase_label: Label = $Hud/SafeArea/PhaseStrip/Margin/Phases/LoopPhase
@onready var run_phase_label: Label = $Hud/SafeArea/PhaseStrip/Margin/Phases/RunPhase
@onready var hint_button: Button = $Hud/SafeArea/ToolRail/HintButton
@onready var tool_rail: Control = $Hud/SafeArea/ToolRail
@onready var completion_card: Control = $Hud/SafeArea/CompletionCard
@onready var growth_feedback: Control = $Hud/SafeArea/GrowthFeedback
@onready var code_drawer: Control = $Hud/CodeDrawer
@onready var drawer_save_state: Label = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/DrawerHeader/SaveState
@onready var blank_hint: Label = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/BlankHint
@onready var start_input: LineEdit = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeFillPanel/CodeMargin/CodeLines/LoopLine/StartInput
@onready var limit_input: LineEdit = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeFillPanel/CodeMargin/CodeLines/LoopLine/LimitInput
@onready var index_input: LineEdit = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeFillPanel/CodeMargin/CodeLines/OutputLine/IndexInput
@onready var trace_label: RichTextLabel = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Trace
@onready var feedback_label: Label = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Feedback
@onready var reset_button: Button = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Actions/ResetButton
@onready var submit_button: Button = $Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Actions/SubmitButton
@onready var spell_overlay: Control = $Hud/MagicSpellOverlay
@onready var spell_flash: ColorRect = $Hud/MagicSpellOverlay/Flash
@onready var spell_title: RichTextLabel = $Hud/MagicSpellOverlay/SpellTitle
@onready var spell_ripple_outer: Control = $Hud/MagicSpellOverlay/RippleOuter
@onready var spell_ripple_inner: Control = $Hud/MagicSpellOverlay/RippleInner
@onready var spell_drops: Label = $Hud/MagicSpellOverlay/WaterDrops
@onready var story_dialogue: StoryDialogueOverlay = $Hud/StoryDialogueOverlay
@onready var autosave_timer: Timer = $AutoSaveTimer
@onready var save_indicator_timer: Timer = $SaveIndicatorTimer

var _phase := Phase.MANUAL_WATERING
var _manual_next_index := 0
var _manual_commands: Array[String] = []
var _manual_action_locked := true
var _saved_draft := STARTER_CODE
var _last_error_kind := ""
var _same_boundary_error_streak := 0
var _drawer_tween: Tween
var _walnut_tween: Tween
var _hud_focus_tween: Tween
var _variable_badge_tween: Tween
var _growth_tween: Tween
var _blank_hint_tween: Tween
var _spell_tween: Tween
var _spell_loop_tween: Tween
var _control_tweens: Dictionary = {}
var _suppress_fill_signal := false
var _spell_drops_rest_y := 0.0


func _ready() -> void:
	for plot in manual_row.get_children():
		(plot as WateringPlot).plot_pressed.connect(manual_water_plot)
		(plot as WateringPlot).set_interactive(false)
	for plot in auto_row.get_children():
		(plot as WateringPlot).set_interactive(false)
	hint_button.pressed.connect(_on_hint_button_pressed)
	reset_button.pressed.connect(reset_code_to_starter)
	submit_button.pressed.connect(request_submit_and_run)
	for input in [start_input, limit_input, index_input]:
		(input as LineEdit).text_changed.connect(_on_fill_changed)
	autosave_timer.timeout.connect(flush_autosave)
	save_indicator_timer.timeout.connect(_hide_save_indicator)
	auto_row.visible = false
	manual_watering_can.visible = false
	magic_watering_can.visible = false
	bug_character.visible = false
	shu_shu.visible = false
	completion_card.visible = false
	growth_feedback.visible = false
	code_drawer.visible = false
	drawer_save_state.visible = false
	variable_badge.visible = false
	spell_overlay.visible = false
	_spell_drops_rest_y = spell_drops.position.y
	_set_phase(Phase.MANUAL_WATERING)
	_hide_manual_guidance()
	_play_hud_intro()
	call_deferred("_play_intro_story")


func get_phase_name() -> String:
	return Phase.keys()[_phase]


func get_saved_draft() -> String:
	return _saved_draft


func get_fill_values() -> Array[String]:
	return [start_input.text, limit_input.text, index_input.text]


func set_fill_values(start_value: String, limit_value: String, index_value: String) -> void:
	_suppress_fill_signal = true
	start_input.text = start_value
	limit_input.text = limit_value
	index_input.text = index_value
	_suppress_fill_signal = false
	_update_fill_hint()


func is_story_dialogue_visible() -> bool:
	return story_dialogue.visible


func skip_story_dialogue() -> void:
	story_dialogue.skip_sequence()


func manual_water_plot(plot_index: int) -> bool:
	if _phase != Phase.MANUAL_WATERING or _manual_action_locked:
		return false
	if plot_index != _manual_next_index:
		(manual_row.get_child(_manual_next_index) as WateringPlot).pulse_guide()
		return false
	_manual_action_locked = true
	var plot := manual_row.get_child(plot_index) as WateringPlot
	plot.set_interactive(false)
	plot.set_guided(false)
	_perform_manual_watering(plot, plot_index)
	return true


func complete_tech_unlock() -> void:
	if _phase != Phase.DISCOVER_REPEAT:
		return
	if story_dialogue.visible:
		story_dialogue.skip_sequence()
	_enter_code_challenge()


func show_code_drawer() -> void:
	if _phase < Phase.CODE_CHALLENGE:
		return
	if _drawer_tween != null and _drawer_tween.is_valid():
		_drawer_tween.kill()
	code_drawer.visible = true
	code_drawer.mouse_filter = Control.MOUSE_FILTER_STOP
	code_drawer.position.x = _get_drawer_rest_x()
	code_drawer.modulate.a = 0.0
	_drawer_tween = create_tween().set_parallel(true)
	_drawer_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_drawer_tween.tween_property(code_drawer, "modulate:a", 1.0, _duration(0.22))
	_drawer_tween.tween_property(code_drawer, "scale", Vector2.ONE, _duration(0.28)).from(Vector2(0.96, 0.96))
	_set_world_hud_weight(0.36)
	_update_fill_hint()
	start_input.grab_focus()


func hide_code_drawer(force: bool = false, immediate: bool = false) -> void:
	if not code_drawer.visible:
		return
	if not force and _phase in [Phase.CODE_CHALLENGE, Phase.RUNNING]:
		start_input.grab_focus()
		return
	if _drawer_tween != null and _drawer_tween.is_valid():
		_drawer_tween.kill()
	if immediate:
		code_drawer.visible = false
		code_drawer.mouse_filter = Control.MOUSE_FILTER_IGNORE
		code_drawer.modulate.a = 1.0
		return
	_drawer_tween = create_tween()
	_drawer_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_IN)
	_drawer_tween.tween_property(code_drawer, "modulate:a", 0.0, _duration(0.16))
	_drawer_tween.tween_callback(func() -> void:
		code_drawer.visible = false
		code_drawer.mouse_filter = Control.MOUSE_FILTER_IGNORE
		code_drawer.modulate.a = 1.0
	)


func flush_autosave() -> void:
	autosave_timer.stop()
	_saved_draft = _build_source_from_fills()
	drawer_save_state.visible = true
	drawer_save_state.text = "✓"
	drawer_save_state.modulate = Color(0.13, 0.36, 0.28, 1)
	save_indicator_timer.start()


func reset_code_to_starter() -> void:
	set_fill_values("", "", "")
	feedback_label.text = "重新来一次：把答案填进三个方格。"
	trace_label.text = "准备好了，填完就按“运行”。"
	flush_autosave()
	_bounce_control(reset_button)


func evaluate_code(source: String) -> Dictionary:
	var loop_pattern := RegEx.new()
	loop_pattern.compile("for\\s*\\(\\s*int\\s+i\\s*=\\s*(-?\\d+)\\s*;\\s*i\\s*<\\s*(-?\\d+)\\s*;\\s*i\\+\\+\\s*\\)")
	var loop_match := loop_pattern.search(source)
	if loop_match == null:
		return _invalid_result("incomplete", "还有方格没填好，先把三个答案补完整吧。")
	var start := int(loop_match.get_string(1))
	var limit := int(loop_match.get_string(2))
	if abs(start) > 20 or abs(limit) > 20 or limit < start:
		return _invalid_result("invalid_range", "这些数字会让水壶走错方向，目标是 0 到 4 号土地。")
	var output_i_pattern := RegEx.new()
	output_i_pattern.compile("cout[^;\\n]*<<\\s*i")
	var output_zero_pattern := RegEx.new()
	output_zero_pattern.compile("cout[^;\\n]*<<\\s*0")
	var uses_i := output_i_pattern.search(source) != null
	var uses_constant_zero := not uses_i and output_zero_pattern.search(source) != null
	if not uses_i and not uses_constant_zero:
		return _invalid_result("missing_output", "第三个方格要填正在变化的土地编号。")
	var variables: Array[int] = []
	var actions: Array[int] = []
	for value in range(start, limit):
		variables.append(value)
		actions.append(value if uses_i else 0)
	var seen := {}
	var duplicates := 0
	var out_of_bounds := 0
	for action in actions:
		if action < 0 or action > 4:
			out_of_bounds += 1
			continue
		if seen.has(action):
			duplicates += 1
		else:
			seen[action] = true
	var missing: Array[int] = []
	for index in range(5):
		if not seen.has(index):
			missing.append(index)
	var passed := missing.is_empty() and duplicates == 0 and out_of_bounds == 0 and actions.size() == 5
	var error_kind := ""
	var message := ""
	if passed:
		message = "咒语正确！书书准备施放循环浇水魔法。"
	elif uses_constant_zero:
		error_kind = "constant_plot"
		message = "大水壶一直回到 0 号土地，第三个方格应该填谁？"
	elif start == 1 and missing == [0]:
		error_kind = "missing_first"
		message = "0 号土地还在等水，第一个方格应该从几开始？"
	elif limit == 4 and missing == [4]:
		error_kind = "missing_last"
		message = "4 号土地还没浇到，第二个方格要让 i 再走一步。"
	elif out_of_bounds > 0:
		error_kind = "out_of_bounds"
		message = "大水壶跑出农田啦，检查前两个方格。"
	else:
		error_kind = "coverage"
		message = "还有土地没喝到水，再看看三个方格。"
	return {
		"passed": passed,
		"error_kind": error_kind,
		"message": message,
		"variables": variables,
		"actions": actions,
		"watered": seen.size(),
		"duplicates": duplicates,
		"out_of_bounds": out_of_bounds,
		"missing": missing,
	}


func submit_code_for_test(source: String) -> Dictionary:
	_saved_draft = source
	var result := evaluate_code(source)
	_apply_simulation_result(result, false)
	return result


func request_submit_and_run() -> void:
	if _phase != Phase.CODE_CHALLENGE:
		return
	flush_autosave()
	_bounce_control(submit_button)
	submit_button.disabled = true
	var result := evaluate_code(_saved_draft)
	if not result.get("passed", false):
		_apply_simulation_result(result, true)
		submit_button.disabled = false
		return
	_set_phase(Phase.RUNNING)
	hide_code_drawer(true)
	feedback_label.text = "书书正在施放循环浇水魔法…"
	task_progress.text = "正在施放循环浇水魔法"
	await _play_loop_magic()
	trace_label.text = "循环魔法开始！\n"
	var actions: Array = result.get("actions", [])
	for plot in auto_row.get_children():
		(plot as WateringPlot).set_watered(false, false)
	for value in actions:
		var plot := auto_row.get_child(int(value)) as WateringPlot
		_show_variable_badge(plot, int(value))
		await _move_magic_can_to_plot(plot)
		await _play_watering_can(magic_watering_can, plot, true)
		plot.set_watered(true, true)
		trace_label.append_text("浇%d号土地中……\n" % int(value))
		await get_tree().create_timer(_duration(0.22)).timeout
	trace_label.append_text("浇灌完成！")
	magic_watering_can.visible = false
	variable_badge.visible = false
	_complete_level(true)
	submit_button.disabled = false


func set_preview_state(state_name: String) -> void:
	if story_dialogue.visible:
		story_dialogue.skip_sequence()
	match state_name:
		"discover":
			_prepare_manual_complete(false)
			_begin_discover_repeat()
		"code":
			_prepare_manual_complete(false)
			_enter_code_challenge()
		"complete":
			_prepare_manual_complete(false)
			_enter_code_challenge()
			submit_code_for_test(CORRECT_CODE)


func _play_intro_story() -> void:
	story_dialogue.play_sequence("芽芽", YAYA_PORTRAIT, INTRO_LINES)
	await story_dialogue.sequence_finished
	if _phase != Phase.MANUAL_WATERING or _manual_next_index > 0:
		return
	_manual_action_locked = false
	(manual_row.get_child(0) as WateringPlot).set_interactive(true)
	_update_manual_guidance(true)
	task_progress.text = "点击土地帮幼苗浇水  0 / 5"


func _perform_manual_watering(plot: WateringPlot, plot_index: int) -> void:
	_move_walnut_to_plot(plot, true)
	await get_tree().create_timer(_duration(0.18)).timeout
	await _play_watering_can(manual_watering_can, plot, false)
	plot.set_watered(true, true)
	_manual_commands.append("浇%d号土地中……" % plot_index)
	_manual_next_index += 1
	task_progress.text = "点击土地帮幼苗浇水  %d / 5" % _manual_next_index
	await get_tree().create_timer(_duration(0.34)).timeout
	if _manual_next_index >= 5:
		_begin_discover_repeat()
		return
	_manual_action_locked = false
	(manual_row.get_child(_manual_next_index) as WateringPlot).set_interactive(true)
	_update_manual_guidance(true)


func _begin_discover_repeat() -> void:
	if _phase not in [Phase.MANUAL_WATERING, Phase.DISCOVER_REPEAT]:
		return
	_set_phase(Phase.DISCOVER_REPEAT)
	_manual_action_locked = true
	auto_row.visible = true
	for plot in auto_row.get_children():
		(plot as WateringPlot).set_watered(false, false)
	_hide_manual_guidance()
	task_progress.text = "第一排完成  ✓"
	if not story_dialogue.visible:
		_play_ding_dang_story()


func _play_ding_dang_story() -> void:
	story_dialogue.play_sequence("叮当师傅", DING_DANG_PORTRAIT, DING_DANG_LINES)
	await story_dialogue.sequence_finished
	_enter_code_challenge()


func _enter_code_challenge() -> void:
	if _phase > Phase.CODE_CHALLENGE or (_phase == Phase.CODE_CHALLENGE and code_drawer.visible):
		return
	_set_phase(Phase.CODE_CHALLENGE)
	auto_row.visible = true
	feedback_label.text = "把 0、5、i 填进三个方格。"
	trace_label.text = "准备好了，填完就按“运行”。"
	show_code_drawer()


func _play_watering_can(can_sprite: AnimatedSprite3D, plot: WateringPlot, large: bool) -> void:
	can_sprite.visible = true
	can_sprite.frame = 0
	can_sprite.speed_scale = _animation_speed(1.0 if not large else 1.35)
	can_sprite.global_position = _watering_can_target(plot, large)
	can_sprite.modulate.a = 0.0
	can_sprite.scale = (Vector3.ONE * 1.22) if large else Vector3.ONE
	var appear := create_tween().set_parallel(true)
	appear.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	appear.tween_property(can_sprite, "modulate:a", 1.0, _duration(0.12))
	appear.tween_property(can_sprite, "scale", (Vector3.ONE * 1.52) if large else Vector3.ONE * 1.08, _duration(0.18))
	await appear.finished
	can_sprite.play(&"pour")
	await can_sprite.animation_finished
	var vanish := create_tween().set_parallel(true)
	vanish.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_IN)
	vanish.tween_property(can_sprite, "modulate:a", 0.0, _duration(0.12))
	vanish.tween_property(can_sprite, "scale", can_sprite.scale * 0.86, _duration(0.12))
	await vanish.finished
	can_sprite.visible = false


func _move_magic_can_to_plot(plot: WateringPlot) -> void:
	var target := _watering_can_target(plot, true)
	if not magic_watering_can.visible:
		magic_watering_can.visible = true
		magic_watering_can.global_position = target + Vector3(-0.7, 0.35, 0.0)
		magic_watering_can.modulate.a = 1.0
	var move := create_tween().set_parallel(true)
	move.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	move.tween_property(magic_watering_can, "global_position", target, _duration(0.28))
	move.tween_property(magic_watering_can, "rotation:z", -0.07, _duration(0.18))
	await move.finished


func _watering_can_target(plot: WateringPlot, large: bool) -> Vector3:
	return plot.global_position + (Vector3(-0.42, 1.70, 0.18) if large else Vector3(-0.26, 1.45, 0.18))


func _play_loop_magic() -> void:
	shu_shu.visible = true
	shu_shu.frame = 0
	shu_shu.speed_scale = _animation_speed(1.0)
	_show_spell_overlay()
	shu_shu.play(&"cast_loop_water")
	await shu_shu.animation_finished
	await get_tree().create_timer(_duration(0.46)).timeout
	await _hide_spell_overlay()


func _show_spell_overlay() -> void:
	if _spell_tween != null and _spell_tween.is_valid():
		_spell_tween.kill()
	if _spell_loop_tween != null and _spell_loop_tween.is_valid():
		_spell_loop_tween.kill()
	spell_overlay.visible = true
	spell_overlay.modulate.a = 0.0
	spell_flash.modulate.a = 0.0
	spell_title.scale = Vector2(0.55, 0.55)
	spell_ripple_outer.scale = Vector2(0.22, 0.22)
	spell_ripple_inner.scale = Vector2(0.38, 0.38)
	spell_drops.modulate.a = 0.0
	_spell_tween = create_tween().set_parallel(true)
	_spell_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_spell_tween.tween_property(spell_overlay, "modulate:a", 1.0, _duration(0.18))
	_spell_tween.tween_property(spell_flash, "modulate:a", 0.34, _duration(0.16))
	_spell_tween.tween_property(spell_title, "scale", Vector2.ONE, _duration(0.42))
	_spell_tween.tween_property(spell_ripple_outer, "scale", Vector2.ONE, _duration(0.52))
	_spell_tween.tween_property(spell_ripple_inner, "scale", Vector2.ONE, _duration(0.44))
	_spell_tween.tween_property(spell_drops, "modulate:a", 1.0, _duration(0.28)).set_delay(_duration(0.12))
	_spell_loop_tween = create_tween().set_loops().set_parallel(true)
	_spell_loop_tween.set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_IN_OUT)
	_spell_loop_tween.tween_property(spell_ripple_outer, "rotation", TAU, _duration(1.8)).as_relative()
	_spell_loop_tween.tween_property(spell_ripple_inner, "rotation", -TAU, _duration(1.4)).as_relative()
	spell_drops.position.y = _spell_drops_rest_y
	_spell_loop_tween.tween_property(spell_drops, "position:y", _spell_drops_rest_y - 14.0, _duration(0.7))
	_spell_loop_tween.chain().tween_property(spell_drops, "position:y", _spell_drops_rest_y, _duration(0.7))


func _hide_spell_overlay() -> void:
	if _spell_loop_tween != null and _spell_loop_tween.is_valid():
		_spell_loop_tween.kill()
	if _spell_tween != null and _spell_tween.is_valid():
		_spell_tween.kill()
	_spell_tween = create_tween().set_parallel(true)
	_spell_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_IN)
	_spell_tween.tween_property(spell_overlay, "modulate:a", 0.0, _duration(0.22))
	_spell_tween.tween_property(spell_title, "scale", Vector2(1.18, 1.18), _duration(0.22))
	await _spell_tween.finished
	spell_overlay.visible = false
	spell_overlay.modulate.a = 1.0


func _apply_simulation_result(result: Dictionary, animate: bool) -> void:
	feedback_label.text = str(result.get("message", ""))
	trace_label.text = _format_terminal_output(result)
	if result.get("passed", false):
		_last_error_kind = ""
		_same_boundary_error_streak = 0
		bug_character.visible = false
		auto_row.visible = true
		for plot in auto_row.get_children():
			(plot as WateringPlot).set_watered(true, animate)
		_complete_level(animate)
		return
	_set_phase(Phase.CODE_CHALLENGE)
	var error_kind := str(result.get("error_kind", ""))
	if error_kind == "missing_last":
		_same_boundary_error_streak = _same_boundary_error_streak + 1 if _last_error_kind == error_kind else 1
	else:
		_same_boundary_error_streak = 0
	_last_error_kind = error_kind
	bug_character.visible = _same_boundary_error_streak >= 2
	if bug_character.visible:
		feedback_label.text = "⚠ 4 号土地还没喝到水，第二个方格再想一想。"
	_bounce_control(feedback_label)


func _complete_level(animate: bool) -> void:
	_set_phase(Phase.COMPLETED)
	if _blank_hint_tween != null and _blank_hint_tween.is_valid():
		_blank_hint_tween.kill()
	hide_code_drawer(true, not animate)
	spell_overlay.visible = false
	manual_path.visible = false
	completion_card.visible = false
	shu_shu.visible = true
	little_walnut.modulate.a = 1.0
	for plot in auto_row.get_children():
		(plot as WateringPlot).raise_leaves(animate)
	task_progress.text = "完成  ✓ 循环浇水魔法"
	_set_world_hud_weight(0.24)
	if animate:
		_show_growth_feedback()
	else:
		_reveal_completion_card()


func _show_growth_feedback() -> void:
	if _growth_tween != null and _growth_tween.is_valid():
		_growth_tween.kill()
	growth_feedback.visible = true
	growth_feedback.modulate.a = 0.0
	growth_feedback.scale = Vector2(0.72, 0.72)
	_growth_tween = create_tween().set_parallel(true)
	_growth_tween.set_trans(Tween.TRANS_ELASTIC).set_ease(Tween.EASE_OUT)
	_growth_tween.tween_property(growth_feedback, "modulate:a", 1.0, _duration(0.20))
	_growth_tween.tween_property(growth_feedback, "scale", Vector2.ONE, _duration(0.48))
	_growth_tween.chain().tween_interval(_duration(0.85))
	_growth_tween.tween_property(growth_feedback, "modulate:a", 0.0, _duration(0.18))
	_growth_tween.chain().tween_callback(_reveal_completion_card)


func _reveal_completion_card() -> void:
	growth_feedback.visible = false
	completion_card.visible = true
	completion_card.modulate.a = 1.0
	completion_card.scale = Vector2.ONE
	_bounce_control(completion_card)


func _invalid_result(error_kind: String, message: String) -> Dictionary:
	return {
		"passed": false,
		"error_kind": error_kind,
		"message": message,
		"variables": [],
		"actions": [],
		"watered": 0,
		"duplicates": 0,
		"out_of_bounds": 0,
		"missing": [0, 1, 2, 3, 4],
	}


func _format_terminal_output(result: Dictionary) -> String:
	if not result.get("passed", false):
		return "还没有施法：%s" % str(result.get("message", "检查一下三个方格。"))
	var lines: Array[String] = ["循环浇水魔法准备完成！"]
	for value in result.get("actions", []):
		lines.append("浇%d号土地中……" % int(value))
	lines.append("浇灌完成！")
	return "\n".join(lines)


func _set_phase(value: Phase) -> void:
	_phase = value
	manual_phase_label.text = "● 体验" if value == Phase.MANUAL_WATERING else "✓"
	loop_phase_label.text = "● 循环" if value in [Phase.DISCOVER_REPEAT, Phase.CODE_CHALLENGE] else ("✓" if value > Phase.CODE_CHALLENGE else "○")
	run_phase_label.text = "● 魔法" if value == Phase.RUNNING else ("✓ 完成" if value == Phase.COMPLETED else "○")


func _update_manual_guidance(animate: bool) -> void:
	var path_parts: Array[String] = []
	for index in range(manual_row.get_child_count()):
		var plot := manual_row.get_child(index) as WateringPlot
		var is_target := index == _manual_next_index and _manual_next_index < 5
		plot.set_guided(is_target, str(index), animate and is_target)
		if index < _manual_next_index:
			path_parts.append("✓%d" % index)
		elif is_target:
			path_parts.append("[%d]" % index)
		else:
			path_parts.append(str(index))
	manual_path.visible = true
	manual_path.text = " → ".join(path_parts)
	manual_path.modulate = Color(1.0, 0.84, 0.39, 1.0)


func _hide_manual_guidance() -> void:
	manual_path.visible = false
	for plot in manual_row.get_children():
		(plot as WateringPlot).set_guided(false)


func _show_variable_badge(plot: WateringPlot, value: int) -> void:
	if _variable_badge_tween != null and _variable_badge_tween.is_valid():
		_variable_badge_tween.kill()
	variable_badge.visible = true
	variable_badge.text = "i = %d\n▼" % value
	var target := plot.global_position + Vector3(0.0, 1.78, 0.0)
	variable_badge.global_position = target + Vector3(0.0, 0.20, 0.0)
	variable_badge.modulate.a = 0.0
	_variable_badge_tween = create_tween().set_parallel(true)
	_variable_badge_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_variable_badge_tween.tween_property(variable_badge, "global_position", target, _duration(0.22))
	_variable_badge_tween.tween_property(variable_badge, "modulate:a", 1.0, _duration(0.14))


func _set_world_hud_weight(target_alpha: float) -> void:
	if _hud_focus_tween != null and _hud_focus_tween.is_valid():
		_hud_focus_tween.kill()
	_hud_focus_tween = create_tween().set_parallel(true)
	_hud_focus_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_hud_focus_tween.tween_property(task_card, "modulate:a", target_alpha, _duration(0.18))
	_hud_focus_tween.tween_property(phase_strip, "modulate:a", target_alpha, _duration(0.18))
	_hud_focus_tween.tween_property(tool_rail, "modulate:a", maxf(target_alpha, 0.35), _duration(0.18))


func _hide_save_indicator() -> void:
	drawer_save_state.visible = false


func _update_fill_hint() -> void:
	var remaining := 0
	for input in [start_input, limit_input, index_input]:
		if (input as LineEdit).text.strip_edges().is_empty():
			remaining += 1
	var filled := 3 - remaining
	task_progress.text = "补全 for 循环  %d / 3" % filled
	if _blank_hint_tween != null and _blank_hint_tween.is_valid():
		_blank_hint_tween.kill()
	blank_hint.modulate.a = 1.0
	if remaining == 0:
		blank_hint.text = "✓ 三个方格填好了，试着运行吧！"
		blank_hint.modulate = Color(0.18, 0.52, 0.34, 1.0)
		return
	blank_hint.text = "✨ 像填空题一样填入答案  （还剩 %d 格）" % remaining
	blank_hint.modulate = Color(0.95, 0.48, 0.16, 1.0)
	_blank_hint_tween = create_tween().set_loops()
	_blank_hint_tween.set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_IN_OUT)
	_blank_hint_tween.tween_property(blank_hint, "modulate:a", 0.46, _duration(0.52))
	_blank_hint_tween.tween_property(blank_hint, "modulate:a", 1.0, _duration(0.52))


func _build_source_from_fills() -> String:
	var start_value := start_input.text.strip_edges()
	var limit_value := limit_input.text.strip_edges()
	var index_value := index_input.text.strip_edges()
	return "for (int i = %s; i < %s; i++) {\n    cout << \"浇\" << %s << \"号土地中……\\n\";\n}\ncout << \"浇灌完成！\";" % [
		start_value if not start_value.is_empty() else "__",
		limit_value if not limit_value.is_empty() else "__",
		index_value if not index_value.is_empty() else "__",
	]


func _on_fill_changed(_new_text: String) -> void:
	if _suppress_fill_signal:
		return
	drawer_save_state.visible = true
	drawer_save_state.text = "↻"
	drawer_save_state.modulate = Color(0.73, 0.42, 0.15, 1)
	save_indicator_timer.stop()
	autosave_timer.start()
	_update_fill_hint()


func _move_walnut_to_plot(plot: WateringPlot, animate: bool) -> void:
	if _walnut_tween != null and _walnut_tween.is_valid():
		_walnut_tween.kill()
	var target := little_walnut.position
	target.x = plot.global_position.x - 0.70
	target.z = plot.global_position.z + 0.72
	if not animate:
		little_walnut.position = target
		return
	_walnut_tween = create_tween().set_parallel(true)
	_walnut_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_walnut_tween.tween_property(little_walnut, "position", target, _duration(0.30))
	_walnut_tween.tween_property(little_walnut, "scale", Vector3.ONE * 1.06, _duration(0.16))
	_walnut_tween.chain().tween_property(little_walnut, "scale", Vector3.ONE, _duration(0.12))


func _on_hint_button_pressed() -> void:
	_bounce_control(hint_button)
	if story_dialogue.visible:
		story_dialogue.advance()
		return
	match _phase:
		Phase.MANUAL_WATERING:
			if _manual_next_index < 5:
				(manual_row.get_child(_manual_next_index) as WateringPlot).pulse_guide()
		Phase.CODE_CHALLENGE:
			feedback_label.text = "提示：三个方格依次填 0、5、i。"
			_bounce_control(blank_hint)
		_:
			pass


func _play_hud_intro() -> void:
	task_card.modulate.a = 0.0
	phase_strip.modulate.a = 0.0
	var task_rest := task_card.position
	task_card.position.x -= 14.0
	var intro := create_tween().set_parallel(true)
	intro.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	intro.tween_property(task_card, "position", task_rest, _duration(0.28))
	intro.tween_property(task_card, "modulate:a", 1.0, _duration(0.24))
	intro.tween_property(phase_strip, "modulate:a", 1.0, _duration(0.22)).set_delay(_duration(0.05))


func _prepare_manual_complete(animate: bool) -> void:
	_manual_next_index = 5
	_manual_action_locked = true
	for index in range(5):
		var plot := manual_row.get_child(index) as WateringPlot
		plot.set_interactive(false)
		plot.set_watered(true, animate)
	_hide_manual_guidance()
	auto_row.visible = true
	_set_phase(Phase.DISCOVER_REPEAT)


func _bounce_control(control: Control) -> void:
	var control_id := control.get_instance_id()
	var previous := _control_tweens.get(control_id) as Tween
	if previous != null and previous.is_valid():
		previous.kill()
	control.pivot_offset = control.size * 0.5
	var tween := create_tween()
	_control_tweens[control_id] = tween
	tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	tween.tween_property(control, "scale", Vector2(0.94, 0.94), _duration(0.06))
	tween.tween_property(control, "scale", Vector2.ONE, _duration(0.16))
	tween.finished.connect(func() -> void:
		if _control_tweens.get(control_id) == tween:
			_control_tweens.erase(control_id)
	)


func _get_drawer_rest_x() -> float:
	return get_viewport().get_visible_rect().size.x - 14.0 - code_drawer.size.x


func _animation_speed(multiplier: float) -> float:
	return multiplier / maxf(0.05, demo_timing_scale)


func _duration(seconds: float) -> float:
	return maxf(0.01, seconds * demo_timing_scale)
