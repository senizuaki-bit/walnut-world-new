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

var _card_tween: Tween
var _scan_tween: Tween
var _attention_tween: Tween
var _soil_state := "severe-dry"
var _attention_active := false
var _error_active := false
var _scanning := false
@onready var soil_art: ArtMotionTexture = %Soil
@onready var soil_glow: ArtMotionTexture = %SoilGlow


func _ready() -> void:
	hit_button.pressed.connect(func() -> void: plot_pressed.emit(plot_index))
	hit_button.mouse_entered.connect(_on_hovered)
	hit_button.mouse_exited.connect(_on_unhovered)
	hit_button.focus_entered.connect(_refresh_glow)
	hit_button.focus_exited.connect(_refresh_glow)
	refresh_data()
	set_result(-1, false)
	water_badge.visibility_changed.connect(func(): $Canvas/ResultPlaque.visible = water_badge.visible)


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
	_update_art(target_moisture - current_moisture, current_moisture > target_moisture + 8)
	_error_active = false
	_refresh_glow()


func _update_art(gap: int, waterlogged := false) -> void:
	_soil_state = "waterlogged" if waterlogged else ("severe-dry" if gap >= 30 else ("light-thirst" if gap > 0 else "target-met"))
	var crop_id: String = {"胡萝卜": "carrot", "番茄": "tomato", "土豆": "potato", "玉米": "corn"}.get(crop_name, "carrot")
	(crop_art as ArtMotionTexture).play_clip("crop-%s-%s-sway" % [crop_id, _soil_state])
	soil_art.play_clip("soil-%s-ambient" % _soil_state if _soil_state in ["target-met", "waterlogged"] else "")
	if _soil_state not in ["target-met", "waterlogged"]:
		soil_art.texture = load("res://assets/art/redesign/crop_adaptive/v2/components/soil-%s.png" % _soil_state)
	_refresh_glow()


func _refresh_glow() -> void:
	if not is_node_ready():
		return
	var mode := "error" if _error_active else ("focus" if hit_button.has_focus() or hit_button.is_hovered() else "selected")
	soil_glow.visible = _error_active or _attention_active or _scanning or hit_button.has_focus() or hit_button.is_hovered()
	if soil_glow.visible:
		soil_glow.play_clip("soil-%s-%s-glow" % [_soil_state, mode])


func show_gap(show_value: bool) -> void:
	gap_label.visible = show_value
	gap_label.text = "缺口 %+d" % (target_moisture - current_moisture)


func set_result(water_units: int, animate: bool = true, is_error: bool = false) -> void:
	_error_active = is_error and water_units >= 0
	_refresh_glow()
	water_badge.visible = water_units >= 0
	$Canvas/ResultPlaque.visible = water_units >= 0
	if water_units < 0:
		water_badge.text = ""
		self_modulate = Color.WHITE
		_update_art(target_moisture - current_moisture, current_moisture > target_moisture + 8)
		return
	water_badge.text = "跳过" if water_units == 0 else ("%d份 · %d ml" % [water_units, water_units * 250])
	water_badge.add_theme_color_override("font_color", Color(0.55, 0.13, 0.07, 1) if is_error else Color(0.06, 0.27, 0.20, 1))
	self_modulate = Color.WHITE
	_update_art(30 if is_error else 0, is_error and current_moisture >= target_moisture)
	if animate:
		_bounce()


func show_candidate_action(amount_ml: int, hydration_after: int, animate: bool = true) -> void:
	water_badge.visible = true
	water_badge.text = "+%d ml · 候选 %d" % [amount_ml, hydration_after]
	water_badge.add_theme_color_override("font_color", Color(0.06, 0.27, 0.20, 1))
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
	water_badge.add_theme_color_override("font_color", Color(0.55, 0.13, 0.07, 1) if is_error else Color(0.06, 0.27, 0.20, 1))
	current_label.text = "候选湿度 %d" % hydration
	moisture_bar.value = clampf(float(hydration) / 100.0, 0.0, 100.0)
	self_modulate = Color(1.0, 0.88, 0.83, 1.0) if is_error else Color.WHITE
	# Presentation statuses come from the candidate evaluator; never write moisture.
	if status == "CORRECT":
		_update_art(0)
	elif status == "UNDERWATERED":
		_update_art(30)
	elif status == "OVERWATERED":
		_update_art(0, true)
	_error_active = is_error
	_refresh_glow()


func reset_candidate_display() -> void:
	refresh_data()
	set_result(-1, false)


func play_scan(duration: float = 0.34) -> void:
	_scanning = true
	_refresh_glow()
	await get_tree().create_timer(duration, false).timeout
	_scanning = false
	_refresh_glow()


func pulse_attention() -> void:
	set_attention(true)
	if _attention_tween != null and _attention_tween.is_valid():
		_attention_tween.kill()
	soil_glow.modulate.a = 1.0
	if bool(Engine.get_meta("art_reduced_motion", false)):
		return
	_attention_tween = create_tween()
	_attention_tween.tween_property(soil_glow, "modulate:a", 0.6, 0.16)
	_attention_tween.tween_property(soil_glow, "modulate:a", 1.0, 0.24)


func set_attention(active: bool) -> void:
	_attention_active = active
	attention_frame.visible = false
	_refresh_glow()


func _bounce() -> void:
	if _card_tween != null and _card_tween.is_valid():
		_card_tween.kill()
	# Keep soil, crop and input hitbox fixed; only emphasize the separate result badge.
	scale = Vector2.ONE
	water_badge.scale = Vector2.ONE
	if bool(Engine.get_meta("art_reduced_motion", false)):
		return
	water_badge.pivot_offset = water_badge.size * 0.5
	_card_tween = create_tween()
	_card_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_card_tween.tween_property(water_badge, "scale", Vector2(1.035, 1.035), 0.18)
	_card_tween.tween_property(water_badge, "scale", Vector2.ONE, 0.16)


func _on_hovered() -> void:
	_refresh_glow()


func _on_unhovered() -> void:
	_refresh_glow()
