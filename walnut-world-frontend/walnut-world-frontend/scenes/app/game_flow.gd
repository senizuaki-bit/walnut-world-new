extends Control

@onready var start_screen: GameStartScreen = %GameStartScreen
@onready var crop_adaptive_watering_demo: CropAdaptiveWateringDemo = %CropAdaptiveWateringDemo
@onready var transition: ColorRect = %Transition

var _transitioning: bool = false


func _ready() -> void:
	start_screen.enter_farm_requested.connect(_enter_farm)
	crop_adaptive_watering_demo.replay_requested.connect(_replay_level)
	crop_adaptive_watering_demo.return_home_requested.connect(_return_home)
	crop_adaptive_watering_demo.next_level_requested.connect(_show_next_level_preview)
	crop_adaptive_watering_demo.visible = false
	transition.visible = false


func _enter_farm() -> void:
	if _transitioning:
		return
	_transitioning = true
	await _fade_to(1.0)
	await start_screen.play_exit()
	crop_adaptive_watering_demo.visible = true
	crop_adaptive_watering_demo.restart_level()
	await _fade_to(0.0)
	_transitioning = false


func _replay_level() -> void:
	if _transitioning:
		return
	_transitioning = true
	await _fade_to(1.0)
	crop_adaptive_watering_demo.restart_level()
	await _fade_to(0.0)
	_transitioning = false


func _return_home() -> void:
	if _transitioning:
		return
	_transitioning = true
	await _fade_to(1.0)
	crop_adaptive_watering_demo.visible = false
	start_screen.play_intro()
	await _fade_to(0.0)
	_transitioning = false


func _show_next_level_preview() -> void:
	crop_adaptive_watering_demo.show_next_level_preview()


func _fade_to(alpha_value: float) -> void:
	transition.visible = true
	transition.mouse_filter = Control.MOUSE_FILTER_STOP
	transition.color = Color(0.08, 0.22, 0.16, transition.color.a)
	var tween := create_tween()
	tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_IN_OUT)
	tween.tween_property(transition, "color:a", alpha_value, 0.26)
	await tween.finished
	if alpha_value <= 0.0:
		transition.visible = false
		transition.mouse_filter = Control.MOUSE_FILTER_IGNORE
