class_name BugLegion2D
extends Control

@onready var members: Array[TextureRect] = [
	$Root/Members/BugCaptain as TextureRect,
	$Root/Members/BugLeft as TextureRect,
	$Root/Members/BugRight as TextureRect,
	$Root/Members/BugRear as TextureRect,
]
@onready var root: Control = $Root
@onready var animation_player: AnimationPlayer = $AnimationPlayer


func _ready() -> void:
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	for member in members:
		member.mouse_filter = Control.MOUSE_FILTER_IGNORE
	animation_player.animation_finished.connect(_on_animation_finished)
	hide_immediately()


func show_legion() -> void:
	animation_player.stop()
	visible = true
	root.modulate = Color(1.0, 1.0, 1.0, 0.0)
	animation_player.play(&"reveal")


func hide_legion() -> void:
	if not visible:
		return
	animation_player.stop()
	animation_player.play(&"dismiss")


func hide_immediately() -> void:
	animation_player.stop()
	root.modulate = Color.WHITE
	visible = false


func is_legion_visible() -> bool:
	return visible


func _on_animation_finished(animation_name: StringName) -> void:
	if animation_name == &"dismiss":
		hide_immediately()
