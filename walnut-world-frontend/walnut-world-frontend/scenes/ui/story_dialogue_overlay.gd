class_name StoryDialogueOverlay
extends Control

signal sequence_finished
signal line_changed(line_index: int, line_text: String)

@export_range(12.0, 80.0, 1.0) var characters_per_second := 34.0

@onready var dimmer: ColorRect = $Dimmer
@onready var avatar_stage: Control = $AvatarStage
@onready var portrait: TextureRect = $AvatarStage/Portrait
@onready var dialogue_card: Control = $DialogueCard
@onready var speaker_label: Label = $DialogueCard/ContentRoot/ContentMargin/Content/Speaker
@onready var response_badge: Label = $DialogueCard/ContentRoot/ContentMargin/Content/ResponseBadge
@onready var body_label: Label = $DialogueCard/ContentRoot/ContentMargin/Content/Body
@onready var question_label: Label = $DialogueCard/ContentRoot/ContentMargin/Content/Question
@onready var continue_hint: Label = $DialogueCard/ContentRoot/ContinueHint
@onready var typewriter_timer: Timer = $TypewriterTimer

var _lines: Array[String] = []
var _line_index := -1
var _typing := false
var _finishing := false
var _hint_tween: Tween
var _transition_tween: Tween
var _card_rest_position := Vector2.ZERO
var _avatar_rest_position := Vector2.ZERO


func _ready() -> void:
	visible = false
	_card_rest_position = dialogue_card.position
	_avatar_rest_position = avatar_stage.position
	typewriter_timer.timeout.connect(_on_typewriter_tick)


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
	_lines = lines.duplicate()
	_line_index = -1
	_typing = false
	_finishing = false
	speaker_label.text = speaker_name
	portrait.texture = portrait_texture
	response_badge.visible = not response_label_text.is_empty()
	response_badge.text = response_label_text
	question_label.visible = not question.is_empty()
	question_label.text = "" if question.is_empty() else "想一想：%s" % question
	continue_hint.visible = false
	modulate.a = 0.0
	visible = true
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


func _advance_to_next_line() -> void:
	_line_index += 1
	if _line_index >= _lines.size():
		_finish_sequence(false)
		return
	if _hint_tween != null and _hint_tween.is_valid():
		_hint_tween.kill()
	continue_hint.visible = false
	continue_hint.scale = Vector2.ONE
	body_label.text = _lines[_line_index]
	body_label.visible_characters = 0
	_typing = true
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
		_finishing = false
		sequence_finished.emit()
		return
	if _transition_tween != null and _transition_tween.is_valid():
		_transition_tween.kill()
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
		sequence_finished.emit()
	)


func _stop_active_tweens() -> void:
	typewriter_timer.stop()
	if _hint_tween != null and _hint_tween.is_valid():
		_hint_tween.kill()
	if _transition_tween != null and _transition_tween.is_valid():
		_transition_tween.kill()
