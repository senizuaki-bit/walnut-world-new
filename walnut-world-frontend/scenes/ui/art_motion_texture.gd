class_name ArtMotionTexture
extends TextureRect
## Presentation only. Never advances world state or emits a gameplay completion.

const ROOT := "res://assets/art/redesign/crop_adaptive/v2/motion/"
@export var motion_id: String = ""
@export var reduced_motion := false
@export var playback_speed := 1.0
@export var hide_when_finished := false
var elapsed_ms := 0.0
var _metadata: Dictionary = {}
var _atlas: AtlasTexture
var _frame_index := -1
var _finished := false
var _pending_atlases: Array[String] = []
static var _registration: Dictionary = {}
var _content_region := Rect2()


func _ready() -> void:
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	visibility_changed.connect(_visibility_changed)
	if not motion_id.is_empty() and is_visible_in_tree():
		play_clip(motion_id, true)
	_visibility_changed()


func play_clip(id: String, restart := false) -> void:
	if id == motion_id and not _metadata.is_empty() and not restart:
		return
	motion_id = id
	elapsed_ms = 0.0
	_frame_index = -1
	_finished = false
	_metadata = {}
	_atlas = null
	if id.is_empty():
		return
	var path := ROOT + "metadata/" + id + ".json"
	if not FileAccess.file_exists(path):
		return
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if not parsed is Dictionary:
		return
	_metadata = parsed
	if _registration.is_empty():
		_registration = JSON.parse_string(FileAccess.get_file_as_string("res://resources/ui/v2/motion-registration.json"))
	var fitted: Array = _registration.get(id, {}).get("rect", [])
	_content_region = Rect2(fitted[0], fitted[1], fitted[2], fitted[3]) if fitted.size() == 4 else Rect2()
	_show_poster()
	# Hidden instances retain their poster and defer expensive atlas loading.
	if not reduced_motion and not bool(Engine.get_meta("art_reduced_motion", false)) and is_visible_in_tree():
		_load_atlas()
	_visibility_changed()


func _load_atlas() -> void:
	if _atlas != null or not _metadata.get("files", {}).has("atlas"):
		return
	var path := ROOT + str(_metadata.files.atlas)
	if path in _pending_atlases:
		return
	if ResourceLoader.load_threaded_request(path, "Texture2D") == OK:
		_pending_atlases.append(path)


func _poll_atlases() -> void:
	for path: String in _pending_atlases.duplicate():
		var status := ResourceLoader.load_threaded_get_status(path)
		if status == ResourceLoader.THREAD_LOAD_IN_PROGRESS:
			continue
		_pending_atlases.erase(path)
		if status != ResourceLoader.THREAD_LOAD_LOADED:
			continue
		var loaded := ResourceLoader.load_threaded_get(path) as Texture2D
		# A late load must not resurrect a previous character/state or hidden effect.
		if not is_visible_in_tree() or reduced_motion or bool(Engine.get_meta("art_reduced_motion", false)):
			continue
		if path != ROOT + str(_metadata.get("files", {}).get("atlas", "")):
			continue
		_atlas = AtlasTexture.new()
		_frame_index = -1
		_atlas.atlas = loaded
		_atlas.filter_clip = true
		_apply_frame(0)
		texture = _atlas


func _visibility_changed() -> void:
	set_process(is_visible_in_tree() or not _pending_atlases.is_empty())
	if not is_visible_in_tree() and _atlas != null:
		_show_poster()
		_atlas = null
		_frame_index = -1
	if is_visible_in_tree() and _metadata.is_empty() and not motion_id.is_empty():
		play_clip(motion_id, true)


func _process(delta: float) -> void:
	_poll_atlases()
	if not is_visible_in_tree():
		set_process(not _pending_atlases.is_empty())
		return
	if _metadata.is_empty():
		return
	if reduced_motion or bool(Engine.get_meta("art_reduced_motion", false)):
		if hide_when_finished:
			visible = false
			return
		if _atlas != null:
			_show_poster()
			_atlas = null
		return
	_load_atlas()
	if _atlas == null or _finished:
		return
	elapsed_ms += delta * 1000.0 * playback_speed
	var duration := float(_metadata.durationMs)
	if elapsed_ms >= duration:
		if bool(_metadata.loop):
			elapsed_ms = fmod(elapsed_ms, duration)
		else:
			elapsed_ms = duration
			_finished = true
			if hide_when_finished:
				visible = false
				return
	var cursor := 0.0
	var frames: Array = _metadata.atlas.frames
	for index in range(frames.size()):
		cursor += float(frames[index].durationMs)
		if elapsed_ms < cursor or index == frames.size() - 1:
			_apply_frame(index)
			break


func _apply_frame(index: int) -> void:
	if index == _frame_index:
		return
	_frame_index = index
	var rect: Array = _metadata.atlas.frames[index].rect
	_atlas.region = Rect2(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
	if _content_region.has_area():
		_atlas.region = Rect2(_atlas.region.position + _content_region.position, _content_region.size)


func _show_poster() -> void:
	var poster := load(ROOT + str(_metadata.files.poster)) as Texture2D
	if _content_region.has_area():
		var fitted := AtlasTexture.new()
		fitted.atlas = poster
		fitted.region = _content_region
		fitted.filter_clip = true
		texture = fitted
	else:
		texture = poster


func _exit_tree() -> void:
	# Balance every threaded request, including clips replaced before loading ended.
	for path in _pending_atlases:
		var status := ResourceLoader.load_threaded_get_status(path)
		if status in [ResourceLoader.THREAD_LOAD_IN_PROGRESS, ResourceLoader.THREAD_LOAD_LOADED]:
			ResourceLoader.load_threaded_get(path)
	_pending_atlases.clear()
