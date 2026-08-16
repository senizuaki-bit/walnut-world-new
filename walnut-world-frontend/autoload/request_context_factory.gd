class_name RequestContextFactory
extends RefCounted

const SCHEMA_VERSION := "1.0.0"


## Bootstrap runs before the client can truthfully form a persisted domain
## RequestContext. It therefore uses the smaller, transport-only identity that
## the Game contract declares for a current HTTP attempt.
static func new_wire_attempt() -> Dictionary:
	var nonce := _attempt_nonce()
	return {
		"schema_version": SCHEMA_VERSION,
		"request_id": "req_client_%s" % nonce,
		"correlation_id": "corr_client_%s" % nonce,
		"trace_id": "trace_client_%s" % nonce,
	}


static func new_attempt(actor: Dictionary, content_ref: Dictionary) -> Dictionary:
	var nonce := _attempt_nonce()
	return {
		"schema_version": SCHEMA_VERSION,
		"request_id": "req_client_%s" % nonce,
		"correlation_id": "corr_client_%s" % nonce,
		"trace_id": "trace_client_%s" % nonce,
		"requested_at": utc_now(),
		"actor": actor.duplicate(true),
		"content_ref": content_ref.duplicate(true),
	}


static func idempotency_key_for(operation: String, business_id: String) -> String:
	var digest := (operation + ":" + business_id).sha256_text().left(32)
	return "idem_%s_%s" % [operation.to_lower(), digest]


static func utc_now() -> String:
	var value := Time.get_datetime_string_from_system(true)
	return value if value.ends_with("Z") else value + "Z"


static func _attempt_nonce() -> String:
	var stamp := utc_now().replace("-", "").replace(":", "").replace("T", "").replace("Z", "")
	return "%s_%s" % [stamp, randi_range(100000, 999999)]
