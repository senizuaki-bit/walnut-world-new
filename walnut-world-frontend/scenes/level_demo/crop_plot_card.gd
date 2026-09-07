class_name CropPlotCard
extends PanelContainer

signal plot_pressed(plot_index: int)

@export_range(0, 7, 1) var plot_index: int = 0
@export var crop_name: String = "胡萝卜"
@export var current_moisture: int = 20
@export var target_moisture: int = 60
@export var crop_texture: Texture2D

@onready var crop_art: TextureRect = %CropArt
@onready var number_label: Label = %NumberLabel
@onready var crop_label: Label = %CropLabel
@onready var moisture_bar: ProgressBar = %MoistureBar
@onready var current_label: Label = %CurrentLabel
@onready var target_label: Label = %TargetLabel
@onready var gap_label: Label = %GapLabel
@onready var water_badge: Label = %WaterBadge
@onready var scan_glow: TextureRect = %ScanGlow
@onready var attention_frame: TextureRect = %AttentionFrame
@onready var hit_button: Button = %HitButton

@onready var soil_art: TextureRect = %Soil
var _selected := false
var _error := false
var _card_tween: Tween
var _scan_tween: Tween
var _attention_tween: Tween


func _ready() -> void:
	hit_button.pressed.connect(func() -> void: plot_pressed.emit(plot_index))
	hit_button.mouse_entered.connect(_on_hovered)
	hit_button.mouse_exited.connect(_on_unhovered)
	hit_button.focus_entered.connect(_refresh_outline)
	hit_button.focus_exited.connect(_refresh_outline)
	refresh_data()
	set_result(-1, false)


func configure(index: int, name_value: String, current_value: int, target_value: int, texture_value: Texture2D) -> void:
	plot_index = index
	crop_name = name_value
	current_moisture = current_value
	target_moisture = target_value
	crop_texture = texture_value
	if is_node_ready():
		refresh_data()


func refresh_data() -> void:
	number_label.text = "%02d" % plot_index
	crop_label.text = crop_name
	current_label.text = "当前湿度 %d" % current_moisture
	target_label.text = "目标湿度 %d" % target_moisture
	moisture_bar.value = current_moisture
	_apply_crop_skin(current_moisture)


func show_gap(show_value: bool) -> void:
	gap_label.visible = show_value
	gap_label.text = "缺口 %+d" % (target_moisture - current_moisture)


func set_result(water_units: int, animate: bool = true, is_error: bool = false) -> void:
	_error = is_error
	_refresh_outline()
	water_badge.visible = water_units >= 0
	if water_units < 0:
		water_badge.text = ""
		self_modulate = Color.WHITE
		return
	water_badge.text = "跳过" if water_units == 0 else ("💧 × %d · %d ml" % [water_units, water_units * 250])
	water_badge.modulate = Color(0.93, 0.25, 0.16, 1) if is_error else Color(0.08, 0.48, 0.42, 1)
	self_modulate = Color(1.0, 0.88, 0.83, 1.0) if is_error else Color.WHITE
	if animate:
		_bounce()


func show_candidate_action(amount_ml: int, hydration_after: int, animate: bool = true) -> void:
	water_badge.visible = true
	water_badge.text = "+%d ml · 候选 %d" % [amount_ml, hydration_after]
	water_badge.modulate = Color(0.08, 0.48, 0.42, 1)
	current_label.text = "候选湿度 %d" % hydration_after
	moisture_bar.value = clampf(float(hydration_after) / 100.0, 0.0, 100.0)
	self_modulate = Color.WHITE
	if animate:
		_bounce()


func show_candidate_outcome(hydration: int, status: String) -> void:
	var label: String = {
		"CORRECT": "符合范围",
		"UNDERWATERED": "水量不足",
		"OVERWATERED": "水量过多",
	}.get(status, "未验证")
	var is_error := status != "CORRECT"
	water_badge.visible = true
	water_badge.text = "候选 %d · %s" % [hydration, label]
	water_badge.modulate = Color(0.93, 0.25, 0.16, 1) if is_error else Color(0.08, 0.48, 0.42, 1)
	_error = is_error
	_refresh_outline()
	_apply_visual_state({"CORRECT": "target-met", "UNDERWATERED": "severe-dry", "OVERWATERED": "waterlogged"}.get(status, "light-thirst"))
	current_label.text = "候选湿度 %d" % hydration
	moisture_bar.value = clampf(float(hydration) / 100.0, 0.0, 100.0)
	self_modulate = Color(1.0, 0.88, 0.83, 1.0) if is_error else Color.WHITE


func reset_candidate_display() -> void:
	refresh_data()
	set_result(-1, false)


func play_scan(duration: float = 0.34) -> void:
	scan_glow.visible = true
	scan_glow.modulate = Color.WHITE
	await get_tree().create_timer(maxf(duration, 0.01)).timeout
	scan_glow.visible = false

func pulse_attention() -> void:
	set_attention(true)

func set_attention(active: bool) -> void:
	_selected = active
	_refresh_outline()

func _refresh_outline() -> void:
	attention_frame.visible = _selected or _error or hit_button.has_focus()
	var color := Color("f8c052")
	if hit_button.has_focus():
		color = Color("45cad5")
	if _error:
		color = Color("e65435")
	(attention_frame.material as ShaderMaterial).set_shader_parameter("outline_color", color)

func _apply_crop_skin(hydration: int) -> void:
	var state := "light-thirst"
	if hydration > target_moisture + 15:
		state = "waterlogged"
	elif hydration >= target_moisture:
		state = "target-met"
	elif hydration < 35 or target_moisture - hydration >= 30:
		state = "severe-dry"
	_apply_visual_state(state)

func _apply_visual_state(state: String) -> void:
	var base := "res://assets/art/redesign/crop_adaptive/v2/components/"
	var species: String = ["carrot", "tomato", "potato", "corn"][plot_index % 4]
	soil_art.texture = load(base + "soil-" + state + ".png")
	crop_art.texture = load(base + "crop-" + species + "-" + state + ".png")
	attention_frame.texture = soil_art.texture
	scan_glow.texture = soil_art.texture

func _bounce() -> void:
	# The source keeps plot bounds fixed; only the crop may react.
	if _card_tween != null and _card_tween.is_valid():
		_card_tween.kill()
	crop_art.modulate = Color(1.08, 1.08, 1.0)
	_card_tween = create_tween()
	_card_tween.tween_property(crop_art, "modulate", Color.WHITE, 0.24)

func _on_hovered() -> void:
	_refresh_outline()

func _on_unhovered() -> void:
	_refresh_outline()
