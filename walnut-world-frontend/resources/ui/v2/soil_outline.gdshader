shader_type canvas_item;
uniform vec4 outline_color : source_color = vec4(1.0, 0.72, 0.17, 1.0);
uniform float thickness = 7.0;
varying vec4 tint;
void vertex() {
	tint = COLOR;
}
void fragment() {
	vec2 d = TEXTURE_PIXEL_SIZE * thickness;
	float center = texture(TEXTURE, UV).a;
	float outer = 0.0;
	float inner = 1.0;
	for (int x = -1; x <= 1; x++) {
		for (int y = -1; y <= 1; y++) {
			float a = texture(TEXTURE, UV + vec2(float(x), float(y)) * d).a;
			outer = max(outer, a);
			inner = min(inner, a);
		}
	}
	COLOR = vec4(outline_color.rgb, (outer - inner) * outline_color.a) * tint;
}
