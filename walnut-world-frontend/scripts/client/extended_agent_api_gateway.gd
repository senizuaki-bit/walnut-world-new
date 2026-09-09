class_name YayaExtendedAgentApiGateway
extends "res://addons/yaya_contract_client/agent_api_gateway.gd"

## Additive gateway surface for resources introduced after the frozen canonical
## Godot client. Existing operations continue through YayaAgentApiGateway.

const BuildRejectionValidator = preload(
	"res://scripts/client/build_rejection_evidence_validator.gd"
)


func get_build_rejection_evidence(
	request_context: Dictionary,
	evidence_id: String,
) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("get_evidence", context_guard.error)
	var guard := _validate_prefixed_id(
		evidence_id, "^evidence_[A-Za-z0-9_-]{8,128}$", "evidence_id",
	)
	if not guard.ok:
		return _reject_request("get_evidence", guard.error)
	return await _dispatch(
		"get_evidence",
		{"request_context": request_context, "evidence_id": evidence_id},
		"build_rejection_evidence",
		-1,
		{"origin_actor": request_context.actor, "evidence_id": evidence_id},
	)


func _validate_success_metadata(
	response_contract: String,
	status: Variant,
	headers: Variant,
	value: Variant,
	attempt_context: Variant,
) -> Dictionary:
	var canonical_contract := (
		"evidence" if response_contract == "build_rejection_evidence" else response_contract
	)
	return super._validate_success_metadata(
		canonical_contract, status, headers, value, attempt_context,
	)


func _validate_response(
	contract_name: String,
	value: Variant,
	expected_after_sequence: int,
	expected_identity: Dictionary,
) -> Dictionary:
	if contract_name == "build_rejection_evidence":
		return _bind_response_identity(
			BuildRejectionValidator.validate(value), value, expected_identity,
		)
	return super._validate_response(
		contract_name, value, expected_after_sequence, expected_identity,
	)
