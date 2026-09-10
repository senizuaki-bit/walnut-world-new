extends Node
## Demo extensions return final HTTP 200 resources, not Command jobs.
@onready var http: HTTPRequest = $HTTPRequest
var base_url := ""
var token := ""
var session_id := ""
var busy := false

func configure(url: String, bearer: String, session: String) -> void:
	base_url = url.trim_suffix("/")
	token = bearer
	session_id = session

func send(action: String, entry_id: String, body: Dictionary = {}) -> Dictionary:
	if busy:
		return {"ok": false, "code": "PRACTICE_BUSY"}
	var payload := JSON.stringify(body)
	if payload.to_utf8_buffer().size() > 40000:
		return {"ok": false, "code": "PRACTICE_REQUEST_TOO_LARGE"}
	busy = true
	var stamp := Crypto.new().generate_random_bytes(16).hex_encode()
	var headers := PackedStringArray([
		"Authorization: Bearer " + token, "Content-Type: application/json",
		"X-Schema-Version: 1.0.0", "X-Request-Id: req_" + stamp,
		"X-Trace-Id: trace_" + stamp, "X-Correlation-Id: corr_" + stamp,
	])
	if action == "answer":
		headers.append("Idempotency-Key: " + str(body.get("answer_id", "")))
	var url := base_url + "/product-experience/v1/sessions/" + session_id.uri_encode() + "/practice-entries/" + entry_id.uri_encode() + "/" + action
	if http.request(url, headers, HTTPClient.METHOD_POST, payload) != OK:
		busy = false
		return {"ok": false, "code": "PRACTICE_CONNECTION_FAILED", "retryable": true}
	var reply: Array = await http.request_completed
	busy = false
	var value: Variant = JSON.parse_string((reply[3] as PackedByteArray).get_string_from_utf8())
	if int(reply[0]) != HTTPRequest.RESULT_SUCCESS or not value is Dictionary:
		return {"ok": false, "code": "PRACTICE_CONNECTION_FAILED", "retryable": true}
	if int(reply[1]) != 200:
		var nested: Variant = value.get("error", {})
		var code := str(value.get("code", nested.get("code", "PRACTICE_UNAVAILABLE") if nested is Dictionary else "PRACTICE_UNAVAILABLE"))
		return {"ok": false, "code": code, "retryable": bool(value.get("retryable", false)), "status": int(reply[1])}
	return {"ok": true, "value": value}

static func answer_body(challenge_id: String, source: String) -> Dictionary:
	return {
		"challenge_id": challenge_id,
		"answer_id": Crypto.new().generate_random_bytes(16).hex_encode(),
		"source_bundle": {"language": "CPP20", "entrypoint": "main.cpp", "files": [
			{"path": "main.cpp", "content": source, "content_sha256": source.sha256_text()},
		]},
	}

static func summary_audio(value: Dictionary, challenge_id: String) -> Dictionary:
	var message := str(value.get("message", ""))
	if message.is_empty() or value.get("challenge_id") != challenge_id or value.get("text_sha256") != message.sha256_text() or value.get("source") != "provider" or value.get("format") != "pcm_s16le" or value.get("sample_rate") != 24000 or value.get("speaker") != "ICL_uranus_zh_male_bujiqingnian_tob":
		return {"ok": false, "code": "PRACTICE_SUMMARY_INVALID"}
	var encoded := str(value.get("audio_base64", ""))
	var pcm := Marshalls.base64_to_raw(encoded)
	if pcm.is_empty() or pcm.size() % 2 != 0 or pcm.size() > 8 * 1024 * 1024 or Marshalls.raw_to_base64(pcm) != encoded:
		return {"ok": false, "code": "BOOK_SPEECH_INVALID_AUDIO"}
	var stream := AudioStreamWAV.new()
	stream.format = AudioStreamWAV.FORMAT_16_BITS
	stream.mix_rate = 24000
	stream.stereo = false
	stream.data = pcm
	return {"ok": true, "stream": stream}
