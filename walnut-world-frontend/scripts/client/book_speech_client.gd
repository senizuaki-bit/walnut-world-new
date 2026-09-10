extends Node
## Fetch a complete, authorized audio asset before exposing its paired summary.
var _base_url := ""
var _token := ""
var _session_id := ""

func configure(base_url: String, token: String, session_id: String) -> void:
	_base_url = base_url.trim_suffix("/")
	_token = token
	_session_id = session_id

func prepare(interaction: Dictionary) -> Dictionary:
	var request := HTTPRequest.new()
	request.timeout = 65.0
	request.body_size_limit = 12 * 1024 * 1024
	request.max_redirects = 0
	add_child(request)
	var stamp := Crypto.new().generate_random_bytes(16).hex_encode()
	var identifier := str(interaction.get("interaction_id", ""))
	var headers := PackedStringArray([
		"Authorization: Bearer " + _token, "X-Schema-Version: 1.0.0",
		"X-Request-Id: req_" + stamp, "X-Trace-Id: trace_" + stamp,
		"X-Correlation-Id: corr_" + stamp,
	])
	var url := _base_url + "/product-experience/v1/sessions/" + _session_id.uri_encode() + "/agent-interactions/" + identifier.uri_encode() + "/speech"
	if request.request(url, headers, HTTPClient.METHOD_POST) != OK:
		request.queue_free()
		return {"ok": false, "code": "BOOK_SPEECH_CONNECTION_FAILED"}
	var reply: Array = await request.request_completed
	request.queue_free()
	var body: Variant = JSON.parse_string((reply[3] as PackedByteArray).get_string_from_utf8())
	if int(reply[0]) != HTTPRequest.RESULT_SUCCESS or int(reply[1]) != 200 or not body is Dictionary:
		return {"ok": false, "code": str(body.get("code", "BOOK_SPEECH_UNAVAILABLE")) if body is Dictionary else "BOOK_SPEECH_UNAVAILABLE"}
	var text := str(interaction.get("feedback", {}).get("message", ""))
	if body.get("interaction_id") != identifier or body.get("text_sha256") != text.sha256_text() or body.get("speaker") != "ICL_uranus_zh_male_bujiqingnian_tob" or body.get("format") != "pcm_s16le" or body.get("sample_rate") != 24000:
		return {"ok": false, "code": "BOOK_SPEECH_IDENTITY_MISMATCH"}
	var pcm := Marshalls.base64_to_raw(str(body.get("audio_base64", "")))
	if pcm.is_empty() or pcm.size() % 2 != 0 or pcm.size() > 8 * 1024 * 1024:
		return {"ok": false, "code": "BOOK_SPEECH_INVALID_AUDIO"}
	var stream := AudioStreamWAV.new()
	stream.format = AudioStreamWAV.FORMAT_16_BITS
	stream.mix_rate = 24000
	stream.stereo = false
	stream.data = pcm
	return {"ok": true, "stream": stream}
