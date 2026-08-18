extends CharacterBody3D

@export var max_speed: float = 4.2
@export var acceleration: float = 18.0
@export var deceleration: float = 22.0

@onready var visual: Node = $Visual
@onready var camera_rig: Node3D = get_parent().get_node_or_null("CameraRig") as Node3D

func _physics_process(delta: float) -> void:
	var input_direction := Input.get_vector(&"move_left", &"move_right", &"move_up", &"move_down")
	if input_direction.length_squared() > 0.0:
		input_direction = input_direction.normalized()

	var movement_direction := Vector3(input_direction.x, 0.0, input_direction.y)
	if camera_rig != null:
		movement_direction = camera_rig.global_transform.basis * movement_direction
		movement_direction.y = 0.0
		movement_direction = movement_direction.normalized()

	var desired_velocity := movement_direction * max_speed
	var rate := acceleration if input_direction.length_squared() > 0.0 else deceleration
	velocity.x = move_toward(velocity.x, desired_velocity.x, rate * delta)
	velocity.z = move_toward(velocity.z, desired_velocity.z, rate * delta)
	velocity.y = 0.0

	visual.call("update_animation", input_direction, input_direction.length_squared() > 0.0)
	move_and_slide()
