extends TextureRect
## The poster is always present; video loads only for the visible scene.

@export var background_id := "B01-farm-background"
@export var reduced_motion := false
@onready var video: VideoStreamPlayer = $Video
var _loaded_id := ""


func _ready() -> void:
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	video.finished.connect(_loop_video)
	visibility_changed.connect(_update_visibility)
	_update_visibility()


func set_background(id: String) -> void:
	if background_id == id and _loaded_id == id:
		return
	background_id = id
	_update_visibility()


func _update_visibility() -> void:
	if not is_node_ready():
		return
	set_process(is_visible_in_tree())
	video.paused = not is_visible_in_tree() or reduced_motion
	if not is_visible_in_tree():
		return
	if _loaded_id != background_id:
		texture = load("res://assets/art/redesign/crop_adaptive/v2/components/" + background_id + ".png")
		video.stop()
		if DisplayServer.get_name() == "headless":
			_loaded_id = background_id
			return
		video.stream = load("res://assets/art/redesign/crop_adaptive/v2/motion/environment/" + background_id + "-ambient.ogv")
		_loaded_id = background_id
		video.play()
	video.visible = not reduced_motion


func _process(_delta: float) -> void:
	var reduce := reduced_motion or bool(Engine.get_meta("art_reduced_motion", false))
	video.paused = reduce
	video.visible = not reduce


func _loop_video() -> void:
	if is_visible_in_tree() and not reduced_motion:
		video.play()
