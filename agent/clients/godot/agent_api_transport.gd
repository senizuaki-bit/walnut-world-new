class_name YayaAgentApiTransport
extends RefCounted

## Adapter port implemented by HTTP, fixture, replay, or offline transports.
##
## execute() is asynchronous and must be awaited. It returns exactly one of:
##   {"ok": true, "status": <HTTP status>, "headers": <normalized headers>, "value": <decoded JSON>}
##   {"ok": false, "status": <HTTP status or 0 locally>, "headers": <normalized headers>, "error": <ErrorResponse or CLIENT_LOCAL error>}
## Header names are case-insensitive at the gateway; adapters must preserve all
## contract headers instead of dropping them after JSON decoding.
##
## A transport must never block the main thread. Implementations which own
## in-flight work should also implement cancel(request_id) and shutdown().

func execute(operation: String, _arguments: Dictionary) -> Dictionary:
	# Keep the port observably asynchronous, including its fail-loud default.
	await Engine.get_main_loop().process_frame
	return _local_failure(
		operation,
		"LOCAL_TRANSPORT_NOT_IMPLEMENTED",
		"The selected Agent API transport has not implemented this operation.",
	)


func cancel(_request_id: String) -> bool:
	return false


func shutdown() -> void:
	pass


func _local_failure(
	operation: String,
	code: String,
	message: String,
	retryable: bool = false,
	category: String = "INTERNAL",
) -> Dictionary:
	return {
		"ok": false,
		"status": 0,
		"headers": {},
		"error": {
			"scope": "CLIENT_LOCAL",
			"code": code,
			"category": category,
			"retryable": retryable,
			"operation": operation,
			"message": message,
		},
	}
