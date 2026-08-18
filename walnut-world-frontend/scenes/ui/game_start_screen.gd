class_name GameStartScreen
extends Control

signal enter_farm_requested

@onready var title_group: Control = %TitleGroup
@onready var hero_card: Control = %HeroCard
@onready var walnut_art: TextureRect = %WalnutArt
@onready var enter_button: Button = %EnterButton
@onready var leaf_left: Label = %LeafLeft
@onready var leaf_right: Label = %LeafRight

var _intro_tween: Tween
var _idle_tween: Tween
var _button_tween: Tween


func _ready() -> void:
	enter_button.pressed.connect(_on_enter_pressed)
	enter_button.mouse_entered.connect(_on_enter_hovered)
	enter_button.mouse_exited.connect(_on_enter_unhovered)
	play_intro()


func play_intro() -> void:
	visible = true
	mouse_filter = Control.MOUSE_FILTER_STOP
	enter_button.disabled = false
	if _intro_tween != null and _intro_tween.is_valid():
		_intro_tween.kill()
	if _idle_tween != null and _idle_tween.is_valid():
		_idle_tween.kill()
	title_group.modulate.a = 0.0
	title_group.position.y += 22.0
	hero_card.modulate.a = 0.0
	hero_card.scale = Vector2(0.88, 0.88)
	walnut_art.modulate.a = 0.0
	walnut_art.position.y += 30.0
	enter_button.modulate.a = 0.0
	leaf_left.modulate.a = 0.0
	leaf_right.modulate.a = 0.0
	_intro_tween = create_tween().set_parallel(true)
	_intro_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_intro_tween.tween_property(title_group, "modulate:a", 1.0, 0.42)
	_intro_tween.tween_property(title_group, "position:y", title_group.position.y - 22.0, 0.48)
	_intro_tween.tween_property(hero_card, "modulate:a", 1.0, 0.40).set_delay(0.10)
	_intro_tween.tween_property(hero_card, "scale", Vector2.ONE, 0.56).set_delay(0.10)
	_intro_tween.tween_property(walnut_art, "modulate:a", 1.0, 0.34).set_delay(0.22)
	_intro_tween.tween_property(walnut_art, "position:y", walnut_art.position.y - 30.0, 0.50).set_delay(0.22)
	_intro_tween.tween_property(enter_button, "modulate:a", 1.0, 0.28).set_delay(0.38)
	_intro_tween.tween_property(leaf_left, "modulate:a", 0.72, 0.30).set_delay(0.28)
	_intro_tween.tween_property(leaf_right, "modulate:a", 0.72, 0.30).set_delay(0.32)
	_intro_tween.finished.connect(_start_idle_motion)


func play_exit() -> void:
	if _intro_tween != null and _intro_tween.is_valid():
		_intro_tween.kill()
	if _idle_tween != null and _idle_tween.is_valid():
		_idle_tween.kill()
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	_intro_tween = create_tween().set_parallel(true)
	_intro_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_IN)
	_intro_tween.tween_property(self, "modulate:a", 0.0, 0.32)
	_intro_tween.tween_property(hero_card, "scale", Vector2(1.04, 1.04), 0.32)
	await _intro_tween.finished
	visible = false
	modulate.a = 1.0


func _start_idle_motion() -> void:
	walnut_art.pivot_offset = walnut_art.size * 0.5
	_idle_tween = create_tween().set_loops().set_parallel(true)
	_idle_tween.set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_IN_OUT)
	_idle_tween.tween_property(walnut_art, "position:y", walnut_art.position.y - 8.0, 1.25)
	_idle_tween.tween_property(walnut_art, "rotation", 0.025, 1.25)
	_idle_tween.chain().set_parallel(true)
	_idle_tween.tween_property(walnut_art, "position:y", walnut_art.position.y, 1.25)
	_idle_tween.tween_property(walnut_art, "rotation", -0.018, 1.25)


func _on_enter_pressed() -> void:
	enter_button.disabled = true
	_bounce_button(Vector2(0.93, 0.93), Vector2(1.04, 1.04))
	enter_farm_requested.emit()


func _on_enter_hovered() -> void:
	if enter_button.disabled:
		return
	_bounce_button(Vector2.ONE, Vector2(1.045, 1.045))


func _on_enter_unhovered() -> void:
	if enter_button.disabled:
		return
	_bounce_button(enter_button.scale, Vector2.ONE)


func _bounce_button(from_scale: Vector2, to_scale: Vector2) -> void:
	if _button_tween != null and _button_tween.is_valid():
		_button_tween.kill()
	enter_button.pivot_offset = enter_button.size * 0.5
	enter_button.scale = from_scale
	_button_tween = create_tween()
	_button_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_button_tween.tween_property(enter_button, "scale", to_scale, 0.18)
