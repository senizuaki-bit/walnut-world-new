extends PanelContainer

const WorldSnapshotProjector = preload("res://scripts/client/world_snapshot_projector.gd")
const HARVEST_DURATION_SECONDS := 0.72

signal authoritative_projection_failed(reason: String)

@onready var status_label: Label = %WorldStatus
@onready var presentation_label: Label = %PresentationStatus
@onready var store: WalnutClientStore = get_node_or_null("/root/ClientStore") as WalnutClientStore
@onready var farm_world: Node = $ViewportShell/SubViewportContainer/SubViewport/FarmWorld

var projector := WorldSnapshotProjector.new()
var _presentation_marker: MeshInstance3D
var _presentation_tween: Tween
var _last_projection_fingerprint := ""
var _last_projection_ok := false


func _ready() -> void:
	if store != null:
		store.world_replaced.connect(_on_world_replaced)
		if not store.world_snapshot.is_empty():
			call_deferred("_on_world_replaced", store.world_snapshot.duplicate(true))


func can_project_authoritative_snapshot(snapshot: Dictionary) -> bool:
	var terrain := farm_world.get_node_or_null("TerrainManager") as TerrainManager
	var avatar := farm_world.get_node_or_null("Player") as Node3D
	return bool(projector.validate(snapshot, terrain, avatar).get("ok", false))


func last_authoritative_projection_succeeded(snapshot: Dictionary) -> bool:
	return _last_projection_ok and _last_projection_fingerprint == _snapshot_fingerprint(snapshot)


## Reprojects only the preauthored visual scene for a current-result replay.
## It deliberately does not emit or write ClientStore World authority.
func project_replay_snapshot(snapshot: Dictionary) -> bool:
	var terrain := farm_world.get_node_or_null("TerrainManager") as TerrainManager
	var avatar := farm_world.get_node_or_null("Player") as Node3D
	if not bool(projector.validate(snapshot, terrain, avatar).get("ok", false)):
		return false
	_clear_presentation_visual()
	return projector.apply(snapshot, terrain, avatar)


func _on_world_replaced(snapshot: Dictionary) -> void:
	var terrain := farm_world.get_node_or_null("TerrainManager") as TerrainManager
	var avatar := farm_world.get_node_or_null("Player") as Node3D
	_last_projection_ok = projector.apply(snapshot, terrain, avatar)
	_last_projection_fingerprint = _snapshot_fingerprint(snapshot) if _last_projection_ok else ""
	if not _last_projection_ok:
		status_label.visible = true
		status_label.text = "权威世界快照无法映射到当前农场场景"
		authoritative_projection_failed.emit(status_label.text)
		return
	status_label.text = "世界 %s · 修订 %s · 事件 %s" % [snapshot.world_id, snapshot.revision, snapshot.last_event_sequence]


func begin_presentation_event(event: Dictionary, speed: float) -> Dictionary:
	if (
		str(event.get("event_type", "")) != "world.action.harvested"
		or int(event.get("event_version", -1)) != 1
		or not event.get("payload") is Dictionary
		or speed not in [1.0, 2.0]
	):
		return {"ok": false, "duration_seconds": 0.0}
	var payload: Dictionary = event.payload
	var position: Variant = payload.get("position")
	var terrain := farm_world.get_node_or_null("TerrainManager") as TerrainManager
	if terrain == null or not position is Dictionary:
		return {"ok": false, "duration_seconds": 0.0}
	var cell := Vector2i(int(position.get("x", -100001)), int(position.get("y", -100001)))
	if not terrain.map_data.is_inside_map(cell):
		return {"ok": false, "duration_seconds": 0.0}
	_clear_presentation_visual()
	_presentation_marker = MeshInstance3D.new()
	_presentation_marker.name = "HarvestPresentationMarker"
	var mesh := SphereMesh.new()
	mesh.radius = 0.24
	mesh.height = 0.48
	_presentation_marker.mesh = mesh
	var material := StandardMaterial3D.new()
	material.albedo_color = Color(1.0, 0.72, 0.18, 1.0)
	material.emission_enabled = true
	material.emission = Color(1.0, 0.42, 0.08, 1.0)
	material.emission_energy_multiplier = 1.6
	_presentation_marker.material_override = material
	_presentation_marker.position = terrain.map_data.cell_to_world(cell) + Vector3(0.0, 0.34, 0.0)
	farm_world.add_child(_presentation_marker)
	presentation_label.visible = true
	presentation_label.text = "权威动作 %s/%s：收获 %s（%s）" % [
		int(event.action_index) + 1, int(event.action_count), str(payload.crop_type), str(payload.plot_id),
	]
	var duration := HARVEST_DURATION_SECONDS / speed
	_presentation_tween = create_tween().set_parallel(true)
	_presentation_tween.set_trans(Tween.TRANS_QUAD).set_ease(Tween.EASE_OUT)
	_presentation_tween.tween_property(_presentation_marker, "position:y", _presentation_marker.position.y + 1.1, duration)
	_presentation_tween.tween_property(_presentation_marker, "scale", Vector3(0.22, 0.22, 0.22), duration)
	return {"ok": true, "duration_seconds": HARVEST_DURATION_SECONDS}


func finish_presentation_event(_event: Dictionary, skipped: bool) -> bool:
	_clear_presentation_visual()
	presentation_label.text = (
		"已跳过权威动作演出；正在恢复最终快照"
		if skipped
		else "权威动作已播放"
	)
	return true


func _clear_presentation_visual() -> void:
	if _presentation_tween != null:
		_presentation_tween.kill()
		_presentation_tween = null
	if is_instance_valid(_presentation_marker):
		_presentation_marker.queue_free()
	_presentation_marker = null


func _snapshot_fingerprint(snapshot: Dictionary) -> String:
	return "%s:%s:%s:%s" % [
		str(snapshot.get("world_id", "")), int(snapshot.get("revision", -1)),
		int(snapshot.get("last_event_sequence", -1)), str(snapshot.get("state_hash", "")),
	]
