class_name YayaBuildRejectionEvidenceValidator
extends RefCounted

## Additive validator for the v0.6 BUILD_REJECTION Evidence resource.
## The canonical Evidence validator remains byte-identical to the frozen Agent
## client.  This validator is selected only for the new additive schema.

const ContractValidator = preload("res://addons/yaya_contract_client/contract_validator.gd")


static func validate(value: Variant) -> Dictionary:
	var shape := ContractValidator._require_shape(value, [
		"request_context", "evidence_ref", "subject", "source", "occurred_at", "recorded_at",
		"integrity", "payload", "related_evidence", "versions",
	], [], "BuildRejectionEvidence")
	if not shape.ok:
		return shape
	for check in [
		ContractValidator.validate_request_context(value.request_context),
		ContractValidator._validate_evidence_ref(value.evidence_ref),
		ContractValidator._validate_date_time(value.occurred_at, "BuildRejectionEvidence.occurred_at"),
		ContractValidator._validate_date_time(value.recorded_at, "BuildRejectionEvidence.recorded_at"),
	]:
		if not check.ok:
			return check

	var subject_shape := ContractValidator._require_shape(
		value.subject, ["learner_id"], [], "BuildRejectionEvidence.subject",
	)
	if not subject_shape.ok:
		return subject_shape
	var learner_check := ContractValidator.validate_identifier(
		value.subject.learner_id, "BuildRejectionEvidence.subject.learner_id",
	)
	if not learner_check.ok:
		return learner_check

	var source_shape := ContractValidator._require_shape(value.source, [
		"source_type", "source_id", "command_id", "world_id",
	], [], "BuildRejectionEvidence.source")
	if not source_shape.ok:
		return source_shape
	if value.source.source_type != "SKILL_BUILD":
		return _failure("BuildRejectionEvidence source must be SKILL_BUILD")
	for check in [
		ContractValidator.validate_identifier(
			value.source.source_id, "BuildRejectionEvidence.source.source_id",
		),
		ContractValidator._validate_pattern(
			value.source.command_id,
			"^cmd_[A-Za-z0-9_-]{8,96}$",
			"BuildRejectionEvidence.source.command_id",
		),
	]:
		if not check.ok:
			return check
	if value.source.world_id != null:
		var world_check := ContractValidator.validate_identifier(
			value.source.world_id, "BuildRejectionEvidence.source.world_id",
		)
		if not world_check.ok:
			return world_check

	var integrity_shape := ContractValidator._require_shape(value.integrity, [
		"payload_sha256", "previous_evidence_sha256",
	], [], "BuildRejectionEvidence.integrity")
	if not integrity_shape.ok:
		return integrity_shape
	var hash_check := ContractValidator._validate_pattern(
		value.integrity.payload_sha256,
		"^[a-f0-9]{64}$",
		"BuildRejectionEvidence.integrity.payload_sha256",
	)
	if not hash_check.ok:
		return hash_check
	if value.integrity.previous_evidence_sha256 != null:
		var previous_hash_check := ContractValidator._validate_pattern(
			value.integrity.previous_evidence_sha256,
			"^[a-f0-9]{64}$",
			"BuildRejectionEvidence.integrity.previous_evidence_sha256",
		)
		if not previous_hash_check.ok:
			return previous_hash_check

	var payload_check := _validate_payload(value.payload)
	if not payload_check.ok:
		return payload_check
	if value.source.source_id != value.payload.build_id:
		return _failure("BuildRejectionEvidence source_id must equal payload.build_id")
	var calculated_hash := ContractValidator.canonical_json_sha256_v1(value.payload)
	if calculated_hash.is_empty() or calculated_hash != value.integrity.payload_sha256:
		return _failure("BuildRejectionEvidence payload hash is invalid")
	if value.evidence_ref.has("sha256") and value.evidence_ref.sha256 != calculated_hash:
		return _failure("BuildRejectionEvidence reference hash is invalid")

	if not value.related_evidence is Array or value.related_evidence.size() > 64:
		return _failure("BuildRejectionEvidence.related_evidence must contain at most 64 items")
	for reference in value.related_evidence:
		var reference_check := ContractValidator._validate_evidence_ref(reference)
		if not reference_check.ok:
			return reference_check
	return ContractValidator._validate_version_set(value.versions)


static func _validate_payload(value: Variant) -> Dictionary:
	var shape := ContractValidator._require_shape(value, [
		"evidence_kind", "build_id", "skill_id", "test_suite_version", "outcome",
		"failure_stage", "failure_code", "diagnostic_codes",
	], [], "BuildRejectionEvidence.payload")
	if not shape.ok:
		return shape
	if value.evidence_kind != "BUILD_REJECTION" or value.outcome != "REJECTED":
		return _failure("BuildRejectionEvidence payload must be a rejected Build")
	for field in ["build_id", "skill_id"]:
		var id_check := ContractValidator.validate_identifier(
			value[field], "BuildRejectionEvidence.payload.%s" % field,
		)
		if not id_check.ok:
			return id_check
	for field in ["test_suite_version", "failure_stage", "failure_code"]:
		if typeof(value[field]) != TYPE_STRING or value[field].is_empty():
			return _failure("BuildRejectionEvidence.payload.%s must be a non-empty string" % field)
	if value.test_suite_version.length() > 96 or value.failure_stage.length() > 64 or value.failure_code.length() > 96:
		return _failure("BuildRejectionEvidence payload field length is invalid")
	return ContractValidator._validate_unique_string_array(
		value.diagnostic_codes, 64, 96, "BuildRejectionEvidence.payload.diagnostic_codes",
	)


static func _failure(message: String) -> Dictionary:
	return {"ok": false, "error": {"message": message}}
