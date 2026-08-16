extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")

const HASH_A := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const HASH_C := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


class FaultInjectingStore:
	extends StoreScript
	var fail_tmp_install := false
	var fail_backup_restore := false
	var fail_target_backup := false
	var fail_backup_remove := false

	func _persistence_rename_absolute(source: String, destination: String) -> Error:
		if fail_tmp_install and source.ends_with(".tmp"):
			return ERR_CANT_CREATE
		if fail_backup_restore and source.ends_with(".bak"):
			return ERR_CANT_CREATE
		if fail_target_backup and destination.ends_with(".bak"):
			return ERR_CANT_CREATE
		return super._persistence_rename_absolute(source, destination)

	func _persistence_remove_absolute(path: String) -> Error:
		if fail_backup_remove and path.ends_with(".bak"):
			return ERR_CANT_CREATE
		return super._persistence_remove_absolute(path)


func _initialize() -> void:
	var existing := root.get_node_or_null("ClientStore")
	if existing != null:
		root.remove_child(existing)
		existing.free()
	var path := "user://client_store_persistence_recovery_test.json"
	var target := ProjectSettings.globalize_path(path)
	var temporary := ProjectSettings.globalize_path("%s.tmp" % path)
	var backup := ProjectSettings.globalize_path("%s.bak" % path)
	_cleanup(target, temporary, backup)

	# Creating the target and then updating it exercises the Windows-safe
	# target -> backup -> temporary -> target path rather than a first write.
	var first := FaultInjectingStore.new()
	first.name = "ClientStore"
	first.persistence_enabled = false
	root.add_child(first)
	await process_frame
	if not first.configure_persistence(path, true, false):
		_abort("Could not configure the recoverable persistence test.", target, temporary, backup)
		return
	if not first.bind_authority("https://api.yaya.example", _bootstrap()).get("ok", false):
		_abort("Could not bind the recoverable persistence authority.", target, temporary, backup)
		return
	first.set_authoritative_bootstrap(_bootstrap())
	var initial_bytes := FileAccess.get_file_as_string(path)
	if initial_bytes.is_empty():
		_abort("The initial authority target was not created.", target, temporary, backup)
		return
	first.set_authoritative_session(_session())
	var updated_bytes := FileAccess.get_file_as_string(path)
	if (
		updated_bytes.is_empty()
		or updated_bytes == initial_bytes
		or FileAccess.file_exists(temporary)
		or FileAccess.file_exists(backup)
	):
		_abort("An existing target update did not commit and clean its exact siblings.", target, temporary, backup)
		return
	# A pre-existing valid backup cannot be deleted unless the valid target is
	# still present. Injecting that delete failure must abort before target moves.
	if not _write_text(backup, updated_bytes):
		_abort("Could not construct the existing target+backup boundary.", target, temporary, backup)
		return
	first.fail_backup_remove = true
	if first._persist_state(true) or FileAccess.get_file_as_string(target) != updated_bytes or FileAccess.get_file_as_string(backup) != updated_bytes or FileAccess.file_exists(temporary):
		_abort("Backup-removal failure lost or advanced the previous authority.", target, temporary, backup)
		return
	first.fail_backup_remove = false
	if not first._persist_state(true) or FileAccess.get_file_as_string(target) != updated_bytes or FileAccess.file_exists(backup) or FileAccess.file_exists(temporary):
		_abort("Existing valid target+backup did not converge through the bounded update protocol.", target, temporary, backup)
		return
	# Failing target -> backup rename must likewise leave target unchanged.
	first.fail_target_backup = true
	if first._persist_state(true) or FileAccess.get_file_as_string(target) != updated_bytes or FileAccess.file_exists(backup) or FileAccess.file_exists(temporary):
		_abort("Target-to-backup rename failure did not retain the valid target.", target, temporary, backup)
		return
	first.fail_target_backup = false

	# Simulate a crash after target has moved to backup. A stale/corrupt staging
	# file is not authority; the valid backup must be restored byte-for-byte.
	_detach(first)
	if DirAccess.rename_absolute(target, backup) != OK or not _write_text(temporary, "{not-json"):
		_abort("Could not construct the backup-only crash boundary.", target, temporary, backup)
		return
	var recovered := await _attach_store(StoreScript.new())
	if not recovered.configure_persistence(path, true, true):
		_abort("A valid backup was not recovered when target was absent.", target, temporary, backup)
		return
	if (
		str(recovered.authoritative_session.get("session_id", "")) != "session_demo_0001"
		or FileAccess.get_file_as_string(path) != updated_bytes
		or FileAccess.file_exists(temporary)
		or FileAccess.file_exists(backup)
	):
		_abort("Backup recovery was not byte-equivalent or left protocol siblings behind.", target, temporary, backup)
		return

	# Fail installation after the old target has become backup, and fail the
	# immediate restore too. The persistence call must fail while leaving the
	# valid backup available to a fresh process.
	_detach(recovered)
	var faulted := await _attach_store(FaultInjectingStore.new()) as FaultInjectingStore
	if not faulted.configure_persistence(path, true, true):
		_abort("Could not load the baseline before fault injection.", target, temporary, backup)
		return
	faulted.last_interaction_sequence = 9
	faulted.fail_tmp_install = true
	faulted.fail_backup_restore = true
	if faulted._persist_state(true):
		_abort("Injected target-install failure was reported as durable success.", target, temporary, backup)
		return
	if FileAccess.file_exists(target) or not FileAccess.file_exists(backup) or FileAccess.get_file_as_string(backup) != updated_bytes:
		_abort("Install/restore failure did not preserve the previous valid authority in backup.", target, temporary, backup)
		return
	_detach(faulted)
	var after_failure := await _attach_store(StoreScript.new())
	if not after_failure.configure_persistence(path, true, true):
		_abort("A fresh process could not recover the authority retained after failure.", target, temporary, backup)
		return
	if (
		after_failure.last_interaction_sequence != 0
		or FileAccess.get_file_as_string(path) != updated_bytes
		or FileAccess.file_exists(temporary)
		or FileAccess.file_exists(backup)
	):
		_abort("Failure recovery exposed uncommitted memory or did not clean exact siblings.", target, temporary, backup)
		return

	# Existing corrupt candidates are explicitly detected. A corrupt backup
	# beside a valid target is strict fail-closed. A corrupt target with a valid
	# backup recovers only from that independently validated backup.
	_detach(after_failure)
	if not _write_text(backup, "{corrupt-backup"):
		_abort("Could not construct the corrupt-backup case.", target, temporary, backup)
		return
	var corrupt_backup_store := await _attach_store(StoreScript.new())
	if (
		corrupt_backup_store.configure_persistence(path, true, true)
		or str(corrupt_backup_store.persistence_integrity_result().get("error", {}).get("code", "")) != "CLIENT_PERSISTENCE_BACKUP_CORRUPT"
	):
		_abort("A corrupt backup was treated as absent beside a valid target.", target, temporary, backup)
		return
	_detach(corrupt_backup_store)
	_cleanup_file(backup)
	if not _write_text(backup, updated_bytes) or not _write_text(target, "{corrupt-target"):
		_abort("Could not construct the corrupt-target case.", target, temporary, backup)
		return
	var corrupt_target_store := await _attach_store(StoreScript.new())
	if (
		not corrupt_target_store.configure_persistence(path, true, true)
		or str(corrupt_target_store.authoritative_session.get("session_id", "")) != "session_demo_0001"
		or FileAccess.get_file_as_string(target) != updated_bytes
		or FileAccess.file_exists(backup)
	):
		_abort("A corrupt target was not recovered byte-for-byte from its valid backup.", target, temporary, backup)
		return

	_detach(corrupt_target_store)
	_cleanup(target, temporary, backup)
	print("CLIENT_STORE_PERSISTENCE_RECOVERY_TEST_PASS")
	quit(0)


func _attach_store(store: WalnutClientStore) -> WalnutClientStore:
	store.name = "ClientStore"
	store.persistence_enabled = false
	root.add_child(store)
	await process_frame
	return store


func _detach(store: Node) -> void:
	root.remove_child(store)
	store.free()


func _write_text(path: String, value: String) -> bool:
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return false
	file.store_string(value)
	file.flush()
	var result := file.get_error() == OK
	file.close()
	return result


func _cleanup(target: String, temporary: String, backup: String) -> void:
	_cleanup_file(target)
	_cleanup_file(temporary)
	_cleanup_file(backup)


func _cleanup_file(path: String) -> void:
	if FileAccess.file_exists(path):
		DirAccess.remove_absolute(path)


func _abort(message: String, target: String, temporary: String, backup: String) -> void:
	_cleanup(target, temporary, backup)
	push_error(message)
	quit(1)


func _bootstrap() -> Dictionary:
	return {
		"actor": {
			"tenant_id": "tenant_demo",
			"actor_id": "learner_demo_0001",
			"actor_type": "student",
			"roles": ["student"],
		},
		"content": {
			"unit_id": "TASK_DEMO_001",
			"version": "1.0.0",
			"content_hash": "d".repeat(64),
		},
		"activation": {
			"scope": {
				"world_id": "world_demo_0001",
				"agent_profile_id": "profile_demo_0001",
			},
			"registry_revision": 3,
			"active": {
				"activation_id": "activation_demo_0001",
				"skill_id": "skill_demo_0001",
				"skill_version_id": "skillver_demo_0001",
				"artifact_sha256": HASH_C,
				"certification_id": "cert_demo_0001",
				"registry_revision": 3,
				"activated_at": "2026-08-12T00:00:00Z",
			},
		},
	}


func _session() -> Dictionary:
	return {
		"session_id": "session_demo_0001",
		"world_id": "world_demo_0001",
		"learner_id": "learner_demo_0001",
		"agent_profile_id": "profile_demo_0001",
		"channel": "GAME",
		"content": _bootstrap().content,
	}
