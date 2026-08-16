class_name YayaStrictJsonObjectScanner
extends RefCounted

## Lightweight lexical pass used before Godot's JSON parser. Godot accepts
## duplicate members and replaces unpaired UTF-16 surrogates; this scanner
## rejects both ambiguities before data reaches the parser or validator.

var _source := ""
var _position := 0
var _duplicate_found := false
var _duplicate_key := ""
var _ill_formed_unicode_found := false


func inspect(source: String) -> Dictionary:
	_source = source
	_position = 0
	_duplicate_found = false
	_duplicate_key = ""
	_ill_formed_unicode_found = false
	_skip_whitespace()
	var valid := _scan_value()
	_skip_whitespace()
	return {
		"ok": valid and _position == _source.length(),
		"duplicate_found": _duplicate_found,
		"duplicate_key": _duplicate_key,
		"ill_formed_unicode_found": _ill_formed_unicode_found,
	}


func _scan_value() -> bool:
	_skip_whitespace()
	if _position >= _source.length():
		return false
	match _character():
		"{":
			return _scan_object()
		"[":
			return _scan_array()
		"\"":
			return _scan_string().ok
		_:
			var start := _position
			while _position < _source.length() and _character() not in [",", "]", "}", " ", "\t", "\r", "\n"]:
				_position += 1
			return _position > start


func _scan_object() -> bool:
	_position += 1
	_skip_whitespace()
	if _consume("}"):
		return true
	var seen := {}
	while _position < _source.length():
		var key_result := _scan_string()
		if not key_result.ok:
			return false
		var key: String = key_result.value
		if seen.has(key) and not _duplicate_found:
			_duplicate_found = true
			_duplicate_key = key
		seen[key] = true
		_skip_whitespace()
		if not _consume(":"):
			return false
		if not _scan_value():
			return false
		_skip_whitespace()
		if _consume("}"):
			return true
		if not _consume(","):
			return false
		_skip_whitespace()
	return false


func _scan_array() -> bool:
	_position += 1
	_skip_whitespace()
	if _consume("]"):
		return true
	while _position < _source.length():
		if not _scan_value():
			return false
		_skip_whitespace()
		if _consume("]"):
			return true
		if not _consume(","):
			return false
		_skip_whitespace()
	return false


func _scan_string() -> Dictionary:
	if not _consume("\""):
		return {"ok": false}
	var start := _position - 1
	while _position < _source.length():
		var character := _character()
		if character == "\\":
			if _position + 1 >= _source.length():
				return {"ok": false}
			var escape := _source.substr(_position + 1, 1)
			if escape == "u":
				var code_unit := _unicode_escape_code_unit(_position)
				if code_unit < 0:
					return {"ok": false}
				if code_unit >= 0xD800 and code_unit <= 0xDBFF:
					var low_surrogate := _unicode_escape_code_unit(_position + 6)
					if low_surrogate < 0xDC00 or low_surrogate > 0xDFFF:
						_ill_formed_unicode_found = true
						return {"ok": false}
					_position += 12
				elif code_unit >= 0xDC00 and code_unit <= 0xDFFF:
					_ill_formed_unicode_found = true
					return {"ok": false}
				else:
					_position += 6
			else:
				_position += 2
			continue
		if character == "\"":
			_position += 1
			var raw := _source.substr(start, _position - start)
			var decoded: Variant = JSON.parse_string(raw)
			return {"ok": typeof(decoded) == TYPE_STRING, "value": decoded}
		_position += 1
	return {"ok": false}


func _unicode_escape_code_unit(position: int) -> int:
	if position < 0 or position + 6 > _source.length():
		return -1
	if _source.substr(position, 2) != "\\u":
		return -1
	var value := 0
	for offset in range(2, 6):
		var character := _source.substr(position + offset, 1)
		var codepoint := character.unicode_at(0)
		var digit := -1
		if codepoint >= 0x30 and codepoint <= 0x39:
			digit = codepoint - 0x30
		elif codepoint >= 0x41 and codepoint <= 0x46:
			digit = codepoint - 0x41 + 10
		elif codepoint >= 0x61 and codepoint <= 0x66:
			digit = codepoint - 0x61 + 10
		if digit < 0:
			return -1
		value = value * 16 + digit
	return value


func _skip_whitespace() -> void:
	while _position < _source.length() and _character() in [" ", "\t", "\r", "\n"]:
		_position += 1


func _consume(expected: String) -> bool:
	if _position >= _source.length() or _character() != expected:
		return false
	_position += 1
	return true


func _character() -> String:
	return _source.substr(_position, 1)
