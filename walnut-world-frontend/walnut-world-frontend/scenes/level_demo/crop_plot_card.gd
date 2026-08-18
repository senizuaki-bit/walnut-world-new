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
@onready var scan_glow: ColorRect = %ScanGlow
@onready var attention_frame: Panel = %AttentionFrame
@onready var hit_button: Button = %HitButton

var _card_tween: Tween
var _scan_tween: Tween
var _attention_tween: Tween


func _ready() -> void:
	hit_button.pressed.connect(func() -> void: plot_pressed.emit(plot_index))
	hit_button.mouse_entered.connect(_on_hovered)
	hit_button.mouse_exited.connect(_on_unhovered)
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
	crop_art.texture = crop_texture


func show_gap(show_value: bool) -> void:
	gap_label.visible = show_value
	gap_label.text = "缺口 %+d" % (target_moisture - current_moisture)


func set_result(water_units: int, animate: bool = true, is_error: bool = false) -> void:
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
	current_label.text = "候选湿度 %d" % hydration
	moisture_bar.value = clampf(float(hydration) / 100.0, 0.0, 100.0)
	self_modulate = Color(1.0, 0.88, 0.83, 1.0) if is_error else Color.WHITE


func reset_candidate_display() -> void:
	refresh_data()
	set_result(-1, false)


func play_scan(duration: float = 0.34) -> void:
	if _scan_tween != null and _scan_tween.is_valid():
		_scan_tween.kill()
	scan_glow.visible = true
	scan_glow.modulate.a = 0.0
	scan_glow.position.x = -size.x * 0.65
	_scan_tween = create_tween().set_parallel(true)
	_scan_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_scan_tween.tween_property(scan_glow, "modulate:a", 0.54, duration * 0.34)
	_scan_tween.tween_property(scan_glow, "position:x", size.x * 0.70, duration)
	_scan_tween.chain().tween_property(scan_glow, "modulate:a", 0.0, duration * 0.22)
	await _scan_tween.finished
	scan_glow.visible = false


func pulse_attention() -> void:
	set_attention(true)
	_bounce()


func set_attention(active: bool) -> void:
	if _attention_tween != null and _attention_tween.is_valid():
		_attention_tween.kill()
	attention_frame.visible = active
	if not active:
		attention_frame.modulate = Color.WHITE
		return
	attention_frame.modulate = Color(1.0, 1.0, 1.0, 0.72)
	_attention_tween = create_tween().set_loops()
	_attention_tween.set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_IN_OUT)
	_attention_tween.tween_property(attention_frame, "modulate:a", 1.0, 0.48)
	_attention_tween.tween_property(attention_frame, "modulate:a", 0.72, 0.48)


func _bounce() -> void:
	if _card_tween != null and _card_tween.is_valid():
		_card_tween.kill()
	pivot_offset = size * 0.5
	scale = Vector2(0.96, 0.96)
	_card_tween = create_tween()
	_card_tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_card_tween.tween_property(self, "scale", Vector2(1.035, 1.035), 0.18)
	_card_tween.tween_property(self, "scale", Vector2.ONE, 0.16)


func _on_hovered() -> void:
	if _card_tween != null and _card_tween.is_valid():
		_card_tween.kill()
	pivot_offset = size * 0.5
	_card_tween = create_tween()
	_card_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_card_tween.tween_property(self, "scale", Vector2(1.025, 1.025), 0.13)


func _on_unhovered() -> void:
	if _card_tween != null and _card_tween.is_valid():
		_card_tween.kill()
	_card_tween = create_tween()
	_card_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_card_tween.tween_property(self, "scale", Vector2.ONE, 0.13)
