extends Node3D

@export_node_path("Node3D") var target_path: NodePath
@export var rotation_step_degrees := 45.0
@export var rotation_smoothing_speed := 12.0
@export var zoom_step := 1.0
@export var minimum_distance := 6.0
@export var maximum_distance := 16.0

var _target: Node3D
var _camera: Camera3D
var _camera_direction := Vector3.ZERO
var _target_rotation_y := 0.0

func _ready() -> void:
	_target = get_node_or_null(target_path) as Node3D
	_camera = get_node_or_null("Camera3D") as Camera3D
	if _camera != null:
		_camera_direction = _camera.position.normalized()
	_target_rotation_y = rotation.y
	_sync_to_target()

func _process(delta: float) -> void:
	_sync_to_target()
	rotation.y = lerp_angle(
		rotation.y,
		_target_rotation_y,
		1.0 - exp(-rotation_smoothing_speed * delta)
	)

func _sync_to_target() -> void:
	if _target == null:
		_target = get_node_or_null(target_path) as Node3D
		if _target == null:
			_target = get_parent().get_node_or_null("Player") as Node3D
		if _target == null:
			return
	global_position = Vector3(_target.global_position.x, 0.0, _target.global_position.z)

func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed(&"camera_rotate_left"):
		rotate_left()
		get_viewport().set_input_as_handled()
	elif event.is_action_pressed(&"camera_rotate_right"):
		rotate_right()
		get_viewport().set_input_as_handled()
	elif event is InputEventMouseButton and event.pressed:
		if event.button_index == MOUSE_BUTTON_WHEEL_UP:
			zoom_by_steps(-1)
			get_viewport().set_input_as_handled()
		elif event.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			zoom_by_steps(1)
			get_viewport().set_input_as_handled()

func rotate_left() -> void:
	_target_rotation_y -= deg_to_rad(rotation_step_degrees)

func rotate_right() -> void:
	_target_rotation_y += deg_to_rad(rotation_step_degrees)

func zoom_by_steps(steps: int) -> void:
	if _camera == null or _camera_direction.is_zero_approx():
		return
	var distance := clampf(
		_camera.position.length() + float(steps) * zoom_step,
		minimum_distance,
		maximum_distance
	)
	_camera.position = _camera_direction * distance
