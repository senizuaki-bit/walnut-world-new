class_name WorldSnapshotRenderer
extends RefCounted

signal snapshot_ready(snapshot: Dictionary)
signal snapshot_rejected(reason: String)

func render(snapshot: Dictionary) -> bool:
	for field in ["world_id", "revision", "last_event_sequence", "state_hash", "state"]:
		if not snapshot.has(field):
			snapshot_rejected.emit("WorldSnapshot 缺少 %s。" % field)
			return false
	if not snapshot.state is Dictionary:
		snapshot_rejected.emit("WorldSnapshot.state 必须是对象。")
		return false
	snapshot_ready.emit(snapshot.duplicate(true))
	return true
