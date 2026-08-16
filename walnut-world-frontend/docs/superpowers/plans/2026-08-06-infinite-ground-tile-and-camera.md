# Infinite Ground Tile and Fixed Follow Camera Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a 1m × 1m, four-way seamless grass ground-tile grid to the world and make the low-angle camera track the player at a fixed relative transform without lag.

**Architecture:** GroundTile.tscn owns one visual 1m PlaneMesh and a shared terrain material. main.tscn owns 400 preconfigured tile instances in a 20×20 grid while the existing single collision plane remains responsible for floor collision. CameraRig remains the camera parent but directly copies the player XZ position, so its child Camera3D retains a fixed local transform.

**Tech Stack:** Godot 4.5.2, GDScript, tscn/tres resources, built-in Image Gen, PNG, Pillow validation, Godot headless smoke test.

## Global Constraints

- Use Godot 4.5.2 and keep the project as a 3D world with billboard characters.
- Use preconfigured scene nodes and static scene instances; do not create the ground grid at runtime.
- Each tile is exactly 1m × 1m; its source texture is exactly 512 × 512 pixels.
- The grass texture is opaque and seamlessly repeatable on all four edges; it contains no terrain boundary, path, object, text, Logo, shadow, or transparent pixels.
- Keep the existing 20m × 20m floor collision and world bounds.
- Camera3D local transform remains (0, 6, 8), rotation (-27°, 0°, 0°), FOV 32°; CameraRig matches player XZ without smoothing.
- All git commits use Chinese messages in the form feat: 提交信息.

---

### Task 1: Generate and validate the seamless base-grass texture

**Files:**
- Create: assets/art/environment/ground_grass_tile.png
- Create: assets/art/environment/ground_grass_tile.png.import
- Create: tmp/imagegen/validate_ground_tile.py
- Modify: docs/art/asset-pipeline.md

**Interfaces:**
- Produces: opaque 512 × 512 PNG at res://assets/art/environment/ground_grass_tile.png.
- Consumed by: resources/materials/ground_grass_tile.tres in Task 2.

- [ ] **Step 1: Write the failing texture validation**

Create tmp/imagegen/validate_ground_tile.py:

~~~
from pathlib import Path
from PIL import Image

tile = Image.open(Path("assets/art/environment/ground_grass_tile.png")).convert("RGBA")
assert tile.size == (512, 512), tile.size
pixels = tile.load()
for x in range(512):
    assert pixels[x, 0][3] == 255 and pixels[x, 511][3] == 255
    assert pixels[x, 0] == pixels[x, 511], "top/bottom seam at x=%d" % x
for y in range(512):
    assert pixels[0, y][3] == 255 and pixels[511, y][3] == 255
    assert pixels[0, y] == pixels[511, y], "left/right seam at y=%d" % y
print("GROUND_TILE_VALID: 512x512, opaque, and four-way seamless")
~~~

- [ ] **Step 2: Run it to verify it fails**

Run: python tmp/imagegen/validate_ground_tile.py

Expected: FileNotFoundError because the terrain PNG does not yet exist.

- [ ] **Step 3: Generate the opaque terrain texture**

Use built-in Image Gen with this prompt:

~~~
Use case: stylized-concept
Asset type: Godot 3D ground-tile texture
Primary request: a single 512×512 square, four-way seamless, opaque hand-painted grass terrain texture for a low-angle pastel fantasy 3D game. It must tile infinitely with top matching bottom and left matching right.
Style/medium: gentle hand-painted illustration, very low contrast, no perspective, no lighting direction, no cast shadows.
Color palette: mint green #8ED8C5, turquoise #54B9B3, pale cyan #CBEDEF, cream white #FFF5D9, muted blue-green #78AFA7.
Materials/textures: soft rounded grass brush marks and sparse evenly distributed tiny tonal variation.
Constraints: fill every pixel; no transparency; no border; no edge vignette; no grass clumps; no rocks; no paths; no flowers; no text; no Logo; no watermark.
~~~

Copy the selected result into assets/art/environment/ground_grass_tile.png and resize to exactly 512 × 512 if needed. Do not chroma-key this full-canvas texture.

- [ ] **Step 4: Validate and import**

Run:

~~~
python tmp/imagegen/validate_ground_tile.py
$engine = 'D:\Godot\Godot_v4.5.2-stable\Godot_v4.5.2-stable_win64_console.exe'
& $engine --headless --path . --editor --import --quit
~~~

Expected: GROUND_TILE_VALID appears and Godot imports the PNG without errors.

- [ ] **Step 5: Document and commit the texture contract**

Append this row to docs/art/asset-pipeline.md:

~~~
| assets/art/environment/ground_grass_tile.png | 512×512、不透明、四向无缝基础草地 | GroundTile.tscn 的 1m×1m 地皮材质 |
~~~

Run:

~~~
git add assets/art/environment/ground_grass_tile.png assets/art/environment/ground_grass_tile.png.import docs/art/asset-pipeline.md
git commit -m "feat: 添加无缝基础草地贴图"
~~~

### Task 2: Add the static 20×20 ground-tile grid

**Files:**
- Create: resources/materials/ground_grass_tile.tres
- Create: scenes/environment/ground_tile.tscn
- Modify: scenes/main/main.tscn
- Modify: tests/smoke_test.gd

**Interfaces:**
- Consumes: res://assets/art/environment/ground_grass_tile.png from Task 1.
- Produces: GroundTile.tscn, a visual-only 1m PlaneMesh instance; Main/GroundTiles with exactly 400 instances named GroundTile_00_00 through GroundTile_19_19.

- [ ] **Step 1: Write the failing terrain-grid assertion**

After the Ground collision assertion in tests/smoke_test.gd, add:

~~~
	var ground_tiles := main.get_node_or_null("GroundTiles") as Node3D
	if ground_tiles == null:
		failures.append("主场景缺少 GroundTiles")
	else:
		if ground_tiles.get_child_count() != 400:
			failures.append("GroundTiles 不是 20×20 的 400 块预置地皮")
		var first_tile := ground_tiles.get_node_or_null("GroundTile_00_00") as Node3D
		var last_tile := ground_tiles.get_node_or_null("GroundTile_19_19") as Node3D
		if first_tile == null or last_tile == null:
			failures.append("地皮命名范围不完整")
		elif not is_equal_approx(first_tile.position.x, -9.5) or not is_equal_approx(first_tile.position.z, -9.5) or not is_equal_approx(last_tile.position.x, 9.5) or not is_equal_approx(last_tile.position.z, 9.5):
			failures.append("地皮网格没有覆盖 -9.5 到 9.5 的中心点")
~~~

- [ ] **Step 2: Run the test to verify it fails**

Run:

~~~
$engine = 'D:\Godot\Godot_v4.5.2-stable\Godot_v4.5.2-stable_win64_console.exe'
& $engine --headless --path . --script res://tests/smoke_test.gd
~~~

Expected: failure reporting that GroundTiles is missing.

- [ ] **Step 3: Create the shared material and visual-only tile scene**

Create resources/materials/ground_grass_tile.tres:

~~~
[gd_resource type="StandardMaterial3D" load_steps=2 format=3]

[ext_resource type="Texture2D" path="res://assets/art/environment/ground_grass_tile.png" id="1_texture"]

[resource]
albedo_texture = ExtResource("1_texture")
roughness = 0.95
metallic = 0.0
texture_filter = 4
~~~

Create scenes/environment/ground_tile.tscn:

~~~
[gd_scene load_steps=3 format=3]

[ext_resource type="Material" path="res://resources/materials/ground_grass_tile.tres" id="1_material"]

[sub_resource type="PlaneMesh" id="PlaneMesh_tile"]
material = ExtResource("1_material")
size = Vector2(1, 1)

[node name="GroundTile" type="Node3D"]

[node name="MeshInstance3D" type="MeshInstance3D" parent="."]
mesh = SubResource("PlaneMesh_tile")
~~~

- [ ] **Step 4: Add 400 static instances**

Add a GroundTiles Node3D under Main and a GroundTile packed-scene external resource to scenes/main/main.tscn. Add one literal static node per row and column using:

~~~
column = 0..19
row = 0..19
name = GroundTile_%02d_%02d
x = -9.5 + column
y = 0.001
z = -9.5 + row
~~~

The tscn must explicitly include these outer instances:

~~~
[node name="GroundTile_00_00" parent="GroundTiles" instance=ExtResource("4_tile")]
position = Vector3(-9.5, 0.001, -9.5)

[node name="GroundTile_19_19" parent="GroundTiles" instance=ExtResource("4_tile")]
position = Vector3(9.5, 0.001, 9.5)
~~~

Keep the existing Ground/MeshInstance3D and Ground/StaticBody3D: it remains the base visual plane and the only floor collider.

- [ ] **Step 5: Run import, test, and commit**

Run:

~~~
$engine = 'D:\Godot\Godot_v4.5.2-stable\Godot_v4.5.2-stable_win64_console.exe'
& $engine --headless --path . --editor --import --quit
& $engine --headless --path . --script res://tests/smoke_test.gd
git add resources/materials/ground_grass_tile.tres scenes/environment/ground_tile.tscn scenes/main/main.tscn tests/smoke_test.gd
git commit -m "feat: 添加预置基础地皮网格"
~~~

Expected: smoke test passes and GroundTiles has 400 static children.

### Task 3: Make CameraRig directly follow player XZ at a fixed camera offset

**Files:**
- Modify: scripts/camera/camera_follow.gd
- Modify: tests/smoke_test.gd
- Modify: docs/architecture/project-structure.md

**Interfaces:**
- Consumes: CameraRig.target_path pointing to Player from scenes/main/main.tscn.
- Produces: CameraRig.global_position equals Player XZ after every process frame; Camera3D local transform never changes.

- [ ] **Step 1: Write the failing fixed-follow assertion**

After the existing Camera3D checks in tests/smoke_test.gd, add:

~~~
	var camera_rig := main.get_node_or_null("CameraRig") as Node3D
	if camera_rig == null:
		failures.append("主场景缺少 CameraRig")
	elif camera == null:
		failures.append("CameraRig 缺少 Camera3D")
	else:
		var camera_local_position := camera.position
		var camera_local_rotation := camera.rotation
		player.global_position = Vector3(3.0, 0.0, -4.0)
		await process_frame
		var expected_rig_position := Vector3(3.0, 0.0, -4.0)
		if not camera_rig.global_position.is_equal_approx(expected_rig_position):
			failures.append("CameraRig 没有与玩家同步 XZ")
		if not camera.position.is_equal_approx(camera_local_position) or not camera.rotation.is_equal_approx(camera_local_rotation):
			failures.append("跟随过程改变了 Camera3D 的固定局部变换")
		player.global_position = Vector3.ZERO
		await process_frame
~~~

- [ ] **Step 2: Run the test to verify it fails with interpolation**

Run: Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/smoke_test.gd

Expected: failure saying CameraRig did not synchronise with the player on the first process frame.

- [ ] **Step 3: Implement direct XZ synchronisation**

Replace scripts/camera/camera_follow.gd with:

~~~
extends Node3D

@export_node_path("Node3D") var target_path: NodePath

var _target: Node3D

func _ready() -> void:
	_target = get_node_or_null(target_path) as Node3D
	_sync_to_target()

func _process(_delta: float) -> void:
	_sync_to_target()

func _sync_to_target() -> void:
	if _target == null:
		return
	global_position = Vector3(_target.global_position.x, 0.0, _target.global_position.z)
~~~

- [ ] **Step 4: Document, verify, and commit**

Append to docs/architecture/project-structure.md:

~~~
CameraRig 只复制 Player 的世界 XZ；Camera3D 保持预置的本地高度、距离、俯角与 FOV。相机不会滞后、旋转或缩放，始终与玩家同步平移。
~~~

Run:

~~~
$engine = 'D:\Godot\Godot_v4.5.2-stable\Godot_v4.5.2-stable_win64_console.exe'
& $engine --headless --path . --script res://tests/smoke_test.gd
git add scripts/camera/camera_follow.gd tests/smoke_test.gd docs/architecture/project-structure.md
git commit -m "feat: 修正固定角度相机同步跟随"
~~~

Expected: SMOKE_TEST_PASS.

### Task 4: Final integrated inspection

**Files:**
- Modify: README.md

**Interfaces:**
- Consumes: completed terrain grid and direct camera follow from Tasks 1–3.
- Produces: documented terrain and camera verification instructions.

- [ ] **Step 1: Add README verification instructions**

Append:

~~~
## 基础地皮与相机验证

- 地皮：主场景使用 20×20 块 1m×1m 的预置 GroundTile，共用 512×512 四向无缝基础草地贴图。
- 相机：CameraRig 同步 Player 的 XZ；Camera3D 保持固定本地距离、俯角和 FOV。
- 验证：Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/smoke_test.gd
~~~

- [ ] **Step 2: Run the full validation and commit**

Run:

~~~
$engine = 'D:\Godot\Godot_v4.5.2-stable\Godot_v4.5.2-stable_win64_console.exe'
& $engine --headless --path . --editor --import --quit
& $engine --headless --path . --script res://tests/smoke_test.gd
git add README.md
git commit -m "feat: 补充地皮与相机验证说明"
~~~

Expected: import exits with code 0 and the test prints SMOKE_TEST_PASS.

## Plan Self-Review

- **Spec coverage:** Task 1 implements the opaque Image Gen grass texture; Task 2 implements the 1m, static 20×20 preconfigured grid while retaining one collision floor; Task 3 implements direct fixed-transform camera following; Task 4 performs integrated verification and documentation.
- **Placeholder scan:** The plan contains no TBD, TODO, deferred implementation, or unspecified path. Resource names, node names, transforms, assertions, and commit messages are explicit.
- **Type consistency:** GroundTile.tscn, GroundTiles, CameraRig, Player, Camera3D and their stated node types match the current scene tree. The test uses only Node3D, Camera3D, Vector3 and APIs available in Godot 4.5.2.
