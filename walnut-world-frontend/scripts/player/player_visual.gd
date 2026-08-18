extends Node3D

@onready var animated_sprite: AnimatedSprite3D = $AnimatedSprite3D

var _facing: StringName = &"down"

func _ready() -> void:
	animated_sprite.play(&"idle_down")

func update_animation(move_direction: Vector2, is_moving: bool) -> void:
	if move_direction.length_squared() > 0.0:
		if absf(move_direction.x) > absf(move_direction.y):
			_facing = &"right" if move_direction.x > 0.0 else &"left"
		else:
			_facing = &"down" if move_direction.y > 0.0 else &"up"

	var prefix := "walk_" if is_moving else "idle_"
	var animation_name := StringName(prefix + String(_facing))
	if animated_sprite.animation != animation_name or not animated_sprite.is_playing():
		animated_sprite.play(animation_name)
