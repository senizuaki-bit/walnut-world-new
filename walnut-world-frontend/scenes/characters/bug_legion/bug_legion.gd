class_name BugLegion
extends Node3D

@export_range(0.01, 1.0, 0.01) var reveal_duration_seconds := 0.24
@export_range(0.01, 1.0, 0.01) var dismiss_duration_seconds := 0.18

@onready var members: Array[Sprite3D] = [
	$Members/BugCaptain as Sprite3D,
	$Members/BugLeft as Sprite3D,
	$Members/BugRight as Sprite3D,
	$Members/BugRear as Sprite3D,
]
@onready var dismiss_timer: Timer = $DismissTimer

var _base_positions: Array[Vector3] = []
var _base_scales: Array[Vector3] = []
var _presentation_tween: Tween


func _ready() -> void:
	for member in members:
		_base_positions.append(member.position)
		_base_scales.append(member.scale)
	dismiss_timer.timeout.connect(_hide_after_dismiss)


func show_challenge() -> void:
	dismiss_timer.stop()
	_stop_presentation_tween()
	visible = true
	_presentation_tween = create_tween().set_parallel(true)
	_presentation_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	for index in members.size():
		var member := members[index]
		member.position = _base_positions[index] + Vector3(0.0, -0.28, 0.0)
		member.scale = _base_scales[index] * 0.72
		member.modulate.a = 0.0
		var delay := float(index) * 0.045
		_presentation_tween.tween_property(member, "position", _base_positions[index], reveal_duration_seconds).set_delay(delay)
		_presentation_tween.tween_property(member, "scale", _base_scales[index], reveal_duration_seconds).set_delay(delay)
		_presentation_tween.tween_property(member, "modulate:a", 1.0, reveal_duration_seconds * 0.7).set_delay(delay)


func dismiss() -> void:
	if not visible:
		return
	_stop_presentation_tween()
	_presentation_tween = create_tween().set_parallel(true)
	_presentation_tween.set_trans(Tween.TRANS_QUAD).set_ease(Tween.EASE_IN)
	for member in members:
		_presentation_tween.tween_property(member, "modulate:a", 0.0, dismiss_duration_seconds)
	dismiss_timer.start(dismiss_duration_seconds)


func _hide_after_dismiss() -> void:
	visible = false
	for index in members.size():
		members[index].position = _base_positions[index]
		members[index].scale = _base_scales[index]
		members[index].modulate.a = 1.0
	_presentation_tween = null


func _stop_presentation_tween() -> void:
	if _presentation_tween != null and _presentation_tween.is_valid():
		_presentation_tween.kill()
	_presentation_tween = null
