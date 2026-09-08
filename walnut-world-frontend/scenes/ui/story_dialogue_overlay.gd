class_name StoryDialogueOverlay
extends Control

signal sequence_finished
signal line_changed(line_index: int, line_text: String)

@export_range(12.0, 80.0, 1.0) var characters_per_second := 34.0

@onready var dimmer: ColorRect = $Dimmer
@onready var avatar_stage: Control = $AvatarStage
@onready var portrait: TextureRect = $AvatarStage/Portrait
@onready var dialogue_card: Control = $DialogueCard
@onready var speaker_label: Label = $DialogueCard/ContentRoot/Speaker
@onready var response_badge: Label = $DialogueCard/ContentRoot/ResponseBadge
@onready var body_label: Label = $DialogueCard/ContentRoot/ContentMargin/Scroll/Content/Body
@onready var question_label: Label = $DialogueCard/ContentRoot/ContentMargin/Scroll/Content/Question
@onready var continue_hint: Label = $DialogueCard/ContentRoot/ContinueHint
@onready var dialogue_scroll: ScrollContainer = $DialogueCard/ContentRoot/ContentMargin/Scroll
@onready var typewriter_timer: Timer = $TypewriterTimer

var _art_role := ""
var _lines: Array[String] = []
var _line_index := -1
var _typing := false
var _finishing := false
var _hint_tween: Tween
var _transition_tween: Tween
var _card_rest_position := Vector2.ZERO
var _avatar_rest_position := Vector2.ZERO
var _previous_focus: WeakRef


func _ready() -> void:
	visible = false
	_card_rest_position = dialogue_card.position
	_avatar_rest_position = avatar_stage.position
	typewriter_timer.timeout.connect(_on_typewriter_tick)
	dialogue_scroll.gui_input.connect(_gui_input)


func play_sequence(speaker_name: String, portrait_texture: Texture2D, lines: Array[String]) -> void:
	_start_sequence(speaker_name, portrait_texture, lines, "", "")


func play_agent_presentation(
	speaker_name: String,
	portrait_texture: Texture2D,
	message: String,
	question: String,
	response_label: String,
) -> void:
	_start_sequence(speaker_name, portrait_texture, [message], question, response_label)


func _start_sequence(
	speaker_name: String,
	portrait_texture: Texture2D,
	lines: Array[String],
	question: String,
	response_label_text: String,
) -> void:
	if lines.is_empty():
		sequence_finished.emit()
		return
	_stop_active_tweens()
	if not visible:
		var owner := get_viewport().gui_get_focus_owner()
		_previous_focus = weakref(owner) if owner != null else null
	_configure_v2_layout(response_label_text.begins_with("L") or response_label_text in ["方向提示", "概念提示", "修改建议", "世界反馈", "成长总结", "目标复述"])
	_lines = lines.duplicate()
	_line_index = -1
	dialogue_scroll.scroll_vertical = 0
	_typing = false
	_finishing = false
	speaker_label.text = speaker_name
	portrait.texture = portrait_texture
	_art_role = {"芽芽": "yaya", "小核桃": "walnut", "叮当师傅": "dingdang", "Bug 先生": "bug", "书书": "shushu", "主角": "player"}.get(speaker_name, "")
	(portrait as ArtMotionTexture).play_clip("" if _art_role.is_empty() else "char-%s-talk" % _art_role)
	response_badge.visible = not response_label_text.is_empty()
	response_badge.text = response_label_text
	question_label.visible = not question.is_empty()
	$DialogueCard/ContentRoot/ContentMargin/Scroll/Content/Divider.visible = question_label.visible
	question_label.text = "" if question.is_empty() else "想一想：%s" % question
	continue_hint.visible = false
	modulate.a = 0.0
	visible = true
	dialogue_card.focus_mode = Control.FOCUS_ALL
	dialogue_card.grab_focus()
	mouse_filter = Control.MOUSE_FILTER_STOP
	avatar_stage.position = _avatar_rest_position + Vector2(0.0, 28.0)
	avatar_stage.scale = Vector2(0.72, 0.72)
	dialogue_card.position = _card_rest_position + Vector2(0.0, 34.0)
	_transition_tween = create_tween().set_parallel(true)
	_transition_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_transition_tween.tween_property(self, "modulate:a", 1.0, 0.20)
	_transition_tween.tween_property(avatar_stage, "position", _avatar_rest_position, 0.34)
	_transition_tween.tween_property(avatar_stage, "scale", Vector2.ONE, 0.38)
	_transition_tween.tween_property(dialogue_card, "position", _card_rest_position, 0.32)
	_advance_to_next_line()


func advance() -> void:
	if not visible or _finishing:
		return
	if _typing:
		typewriter_timer.stop()
		body_label.visible_characters = -1
		_typing = false
		_show_continue_hint()
		return
	if _line_index + 1 < _lines.size():
		_advance_to_next_line()
		return
	_finish_sequence(false)


func skip_sequence() -> void:
	if not visible:
		return
	_finish_sequence(true)


func is_typing() -> bool:
	return _typing


func get_line_index() -> int:
	return _line_index


func _gui_input(event: InputEvent) -> void:
	var mouse_event := event as InputEventMouseButton
	var touch_event := event as InputEventScreenTouch
	if mouse_event != null and mouse_event.button_index == MOUSE_BUTTON_LEFT and mouse_event.pressed:
		advance()
		accept_event()
	elif touch_event != null and touch_event.pressed:
		advance()
		accept_event()


func _input(event: InputEvent) -> void:
	if not is_visible_in_tree():
		return
	if event is InputEventKey or event is InputEventJoypadButton or event is InputEventJoypadMotion:
		# Consume before GUI dispatch: Tab and typing must never reach the lesson behind us.
		get_viewport().set_input_as_handled()
		if event.is_action_pressed("ui_accept") and not event.is_echo():
			advance()
		elif event.is_action_pressed("ui_up") or event.is_action_pressed("ui_down"):
			var scroll := dialogue_scroll
			scroll.scroll_vertical += -48 if event.is_action_pressed("ui_up") else 48
		if visible:
			dialogue_card.grab_focus()


func _restore_focus() -> void:
	var owner: Control = _previous_focus.get_ref() as Control if _previous_focus != null else null
	_previous_focus = null
	if is_instance_valid(owner) and owner.is_visible_in_tree() and owner.focus_mode != Control.FOCUS_NONE:
		owner.grab_focus()


func _advance_to_next_line() -> void:
	_line_index += 1
	if _line_index >= _lines.size():
		_finish_sequence(false)
		return
	if _hint_tween != null and _hint_tween.is_valid():
		_hint_tween.kill()
	continue_hint.visible = false
	continue_hint.scale = Vector2.ONE
	dialogue_scroll.scroll_vertical = 0
	body_label.text = _lines[_line_index]
	body_label.visible_characters = 0
	_typing = true
	if not _art_role.is_empty():
		(portrait as ArtMotionTexture).play_clip("char-%s-talk" % _art_role)
	typewriter_timer.wait_time = 1.0 / characters_per_second
	typewriter_timer.start()
	line_changed.emit(_line_index, body_label.text)
	_pulse_card()


func _on_typewriter_tick() -> void:
	if not _typing:
		typewriter_timer.stop()
		return
	body_label.visible_characters += 1
	if body_label.visible_characters >= body_label.text.length():
		typewriter_timer.stop()
		body_label.visible_characters = -1
		_typing = false
		_show_continue_hint()


func _show_continue_hint() -> void:
	if not _art_role.is_empty():
		(portrait as ArtMotionTexture).play_clip("char-%s-idle" % _art_role)
	if _hint_tween != null and _hint_tween.is_valid():
		_hint_tween.kill()
	continue_hint.visible = true
	continue_hint.pivot_offset = continue_hint.size * 0.5
	continue_hint.scale = Vector2.ONE
	continue_hint.modulate.a = 1.0
	_hint_tween = create_tween().set_loops()
	_hint_tween.set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_IN_OUT)
	_hint_tween.tween_property(continue_hint, "scale", Vector2(1.06, 1.06), 0.42)
	_hint_tween.parallel().tween_property(continue_hint, "modulate:a", 0.78, 0.42)
	_hint_tween.tween_property(continue_hint, "scale", Vector2.ONE, 0.42)
	_hint_tween.parallel().tween_property(continue_hint, "modulate:a", 1.0, 0.42)


func _pulse_card() -> void:
	dialogue_card.pivot_offset = dialogue_card.size * 0.5
	var pulse := create_tween()
	pulse.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	pulse.tween_property(dialogue_card, "scale", Vector2(1.018, 1.018), 0.10)
	pulse.tween_property(dialogue_card, "scale", Vector2.ONE, 0.18)


func _finish_sequence(immediate: bool) -> void:
	if not visible or (_finishing and not immediate):
		return
	# Cancel the old exit callback before an immediate close can emit completion.
	_stop_active_tweens()
	_finishing = true
	_typing = false
	typewriter_timer.stop()
	if _hint_tween != null and _hint_tween.is_valid():
		_hint_tween.kill()
	continue_hint.visible = false
	if immediate:
		visible = false
		mouse_filter = Control.MOUSE_FILTER_IGNORE
		modulate.a = 1.0
		dialogue_card.position = _card_rest_position
		_finishing = false
		_restore_focus()
		sequence_finished.emit()
		return
	_transition_tween = create_tween().set_parallel(true)
	_transition_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_IN)
	_transition_tween.tween_property(self, "modulate:a", 0.0, 0.20)
	_transition_tween.tween_property(dialogue_card, "position:y", _card_rest_position.y + 24.0, 0.20)
	_transition_tween.finished.connect(func() -> void:
		visible = false
		mouse_filter = Control.MOUSE_FILTER_IGNORE
		modulate.a = 1.0
		dialogue_card.position = _card_rest_position
		_finishing = false
		_restore_focus()
		sequence_finished.emit()
	)


func _stop_active_tweens() -> void:
	typewriter_timer.stop()
	if _hint_tween != null and _hint_tween.is_valid():
		_hint_tween.kill()
	if _transition_tween != null and _transition_tween.is_valid():
		_transition_tween.kill()


func _configure_v2_layout(expanded: bool) -> void:
	const K := 720.0 / 941.0
	dialogue_card.position = Vector2(300, 389) * K if expanded else Vector2(280, 608) * K
	dialogue_card.size = Vector2(1160, 472) * K if expanded else Vector2(1125, 284) * K
	$DialogueCard/ContentRoot.size = dialogue_card.size
	avatar_stage.position = Vector2(338, 429) * K if expanded else Vector2(318, 648) * K
	avatar_stage.size = Vector2(156, 392) * K if expanded else Vector2(156, 204) * K
	# Match the guide's nameplate and body offsets; long live messages still scroll.
	var margin := $DialogueCard/ContentRoot/ContentMargin as MarginContainer
	margin.add_theme_constant_override("margin_left", roundi(219 * K))
	margin.add_theme_constant_override("margin_top", roundi(96 * K))
	margin.add_theme_constant_override("margin_right", roundi(59 * K))
	margin.add_theme_constant_override("margin_bottom", roundi(62 * K))
	speaker_label.position = Vector2(203, 28) * K
	speaker_label.size = Vector2(193, 48) * K
	response_badge.position = Vector2(420, 40) * K
	response_badge.size = Vector2(560, 34) * K
	body_label.add_theme_font_size_override("font_size", roundi((25 if expanded else 27) * K))
	body_label.custom_minimum_size.y = (216 if expanded else 91) * K
	question_label.add_theme_font_size_override("font_size", roundi(21 * K))
	continue_hint.text = "▼ 点击继续"
	continue_hint.add_theme_font_size_override("font_size", roundi(23 * K))
	continue_hint.set_anchors_and_offsets_preset(Control.PRESET_TOP_LEFT)
	continue_hint.position = Vector2(dialogue_card.size.x - 214 * K, dialogue_card.size.y - 47 * K)
	continue_hint.size = Vector2(175, 38) * K
	_card_rest_position = dialogue_card.position
	_avatar_rest_position = avatar_stage.position
