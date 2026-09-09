extends RefCounted
## Continuous mono resampling and fixed 20 ms PCM16 frames for Dingdang.
const INPUT_RATE := 16000
const FRAME_BYTES := 640
var _samples := PackedFloat32Array()
var _cursor := 0.0
var _bytes := PackedByteArray()

func clear() -> void:
	_samples.clear()
	_bytes.clear()
	_cursor = 0.0

func encode(frames: PackedVector2Array, mix_rate: float) -> Array[PackedByteArray]:
	var packets: Array[PackedByteArray] = []
	if mix_rate < INPUT_RATE or frames.is_empty():
		return packets
	for frame in frames:
		_samples.append(clampf((frame.x + frame.y) * 0.5, -1.0, 1.0))
	var step := mix_rate / float(INPUT_RATE)
	while int(_cursor) + 1 < _samples.size():
		var index := int(_cursor)
		var value := lerpf(_samples[index], _samples[index + 1], _cursor - index)
		var offset := _bytes.size()
		_bytes.resize(offset + 2)
		_bytes.encode_u16(offset, int(round(value * 32767.0)) & 0xffff)
		_cursor += step
	var consumed := mini(int(_cursor), _samples.size())
	_samples = _samples.slice(consumed)
	_cursor -= consumed
	while _bytes.size() >= FRAME_BYTES:
		packets.append(_bytes.slice(0, FRAME_BYTES))
		_bytes = _bytes.slice(FRAME_BYTES)
	return packets

static func decode(bytes: PackedByteArray) -> PackedVector2Array:
	var frames := PackedVector2Array()
	if bytes.size() % 2 != 0:
		return frames
	frames.resize(bytes.size() / 2)
	for index in frames.size():
		var sample := bytes.decode_u16(index * 2)
		if sample >= 32768:
			sample -= 65536
		var value := float(sample) / 32768.0
		frames[index] = Vector2(value, value)
	return frames
