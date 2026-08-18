class_name WateringPlot
extends Node3D

signal plot_pressed(plot_index: int)

@export_range(0, 4, 1) var plot_index := 0
@export var interactive := true

@onready var tilled_soil: MeshInstance3D = $TilledSoil
@onready var watered_soil: MeshInstance3D = $WateredSoil
@onready var seedling: Sprite3D = $Seedling
@onready var plot_number: Label3D = $PlotNumber
@onready var guide_ring: MeshInstance3D = $GuideRing
@onready var guide_label: Label3D = $GuideLabel
@onready var growth_sparkles: Label3D = $GrowthSparkles
@onready var interaction_area: Area3D = $InteractionArea

var is_watered := false
var _water_tween: Tween
var _guide_tween: Tween
var _rest_seedling_position := Vector3.ZERO
var _rest_seedling_scale := Vector3.ONE
var _guide_label_rest_position := Vector3.ZERO

func _ready() -> void:
	plot_number.text = str(plot_index)
	_rest_seedling_position = seedling.position
	_rest_seedling_scale = seedling.scale
	_guide_label_rest_position = guide_label.position
	interaction_area.input_event.connect(_on_input_event)
	set_watered(false, false)

func set_watered(value: bool, animate := true) -> void:
	if _water_tween != null and _water_tween.is_valid():
		_water_tween.kill()
	is_watered = value
	tilled_soil.visible = not value
	watered_soil.visible = value
	seedling.position = _rest_seedling_position
	seedling.scale = _rest_seedling_scale
	seedling.rotation = Vector3.ZERO
	growth_sparkles.visible = false
	if not value:
		seedling.visible = false
		seedling.modulate.a = 0.0
		return
	seedling.visible = true
	seedling.modulate.a = 1.0
	if not animate:
		return
	seedling.position.y -= 0.28
	seedling.scale = _rest_seedling_scale * 0.08
	seedling.rotation.z = -0.2
	seedling.modulate.a = 0.0
	growth_sparkles.visible = true
	growth_sparkles.scale = Vector3.ONE * 0.25
	growth_sparkles.modulate.a = 1.0
	_water_tween = create_tween().set_parallel(true)
	_water_tween.set_trans(Tween.TRANS_ELASTIC).set_ease(Tween.EASE_OUT)
	_water_tween.tween_property(seedling, "position:y", _rest_seedling_position.y, 0.58)
	_water_tween.tween_property(seedling, "scale", _rest_seedling_scale, 0.58)
	_water_tween.tween_property(seedling, "rotation:z", 0.0, 0.48)
	_water_tween.tween_property(seedling, "modulate:a", 1.0, 0.18)
	_water_tween.tween_property(growth_sparkles, "scale", Vector3.ONE * 1.55, 0.48)
	_water_tween.tween_property(growth_sparkles, "modulate:a", 0.0, 0.52)
	_water_tween.finished.connect(func() -> void: growth_sparkles.visible = false)

func raise_leaves(animate := true) -> void:
	if _water_tween != null and _water_tween.is_valid():
		_water_tween.kill()
	seedling.visible = true
	seedling.modulate.a = 1.0
	var raised_scale := Vector3(_rest_seedling_scale.x * 1.08, _rest_seedling_scale.y * 1.22, _rest_seedling_scale.z)
	if not animate:
		seedling.scale = raised_scale
		seedling.position.y = _rest_seedling_position.y + 0.12
		return
	_water_tween = create_tween().set_parallel(true)
	_water_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_water_tween.tween_property(seedling, "scale", raised_scale, 0.42)
	_water_tween.tween_property(seedling, "position:y", _rest_seedling_position.y + 0.12, 0.42)

func set_interactive(value: bool) -> void:
	interactive = value
	interaction_area.input_ray_pickable = value


func set_guided(value: bool, marker_text: String = "", animate: bool = true) -> void:
	if _guide_tween != null and _guide_tween.is_valid():
		_guide_tween.kill()
	guide_ring.visible = value
	guide_label.visible = value
	if not value:
		guide_ring.scale = Vector3.ONE
		guide_label.position = _guide_label_rest_position
		return
	guide_label.text = "▼\n%s" % (marker_text if not marker_text.is_empty() else str(plot_index))
	guide_ring.scale = Vector3.ONE
	guide_label.position = _guide_label_rest_position
	if not animate:
		return
	pulse_guide()


func pulse_guide() -> void:
	if not guide_ring.visible:
		return
	if _guide_tween != null and _guide_tween.is_valid():
		_guide_tween.kill()
	guide_ring.scale = Vector3(0.78, 0.78, 0.78)
	guide_label.position = _guide_label_rest_position + Vector3(0.0, 0.12, 0.0)
	_guide_tween = create_tween().set_parallel(true)
	_guide_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_guide_tween.tween_property(guide_ring, "scale", Vector3(1.16, 1.16, 1.16), 0.18)
	_guide_tween.tween_property(guide_label, "position", _guide_label_rest_position, 0.22)
	_guide_tween.chain().tween_property(guide_ring, "scale", Vector3.ONE, 0.16).set_trans(Tween.TRANS_CUBIC)


func is_guided() -> bool:
	return guide_ring.visible and guide_label.visible

func _on_input_event(_camera: Node, event: InputEvent, _event_position: Vector3, _normal: Vector3, _shape_idx: int) -> void:
	if not interactive:
		return
	var mouse_event := event as InputEventMouseButton
	if mouse_event != null and mouse_event.button_index == MOUSE_BUTTON_LEFT and mouse_event.pressed:
		plot_pressed.emit(plot_index)
