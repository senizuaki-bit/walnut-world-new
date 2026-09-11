extends RefCounted
## Real-provider tests must never share the normal player's gateway.

static func allowed(url: String, player_binding: Dictionary, dedicated_gateway: bool) -> bool:
	var origin := _local_origin(url)
	var player_origin := _local_origin(str(player_binding.get("api_base_url", "")))
	return dedicated_gateway and not origin.is_empty() and origin != "http://127.0.0.1:8790" and origin != player_origin

static func _local_origin(url: String) -> String:
	var pattern := RegEx.new()
	pattern.compile("^http://(127\\.0\\.0\\.1|localhost):([0-9]+)/?$")
	var matched := pattern.search(url)
	if matched == null: return ""
	var port := matched.get_string(2).to_int()
	if port < 1024 or port > 65535: return ""
	return "http://127.0.0.1:%d" % port
