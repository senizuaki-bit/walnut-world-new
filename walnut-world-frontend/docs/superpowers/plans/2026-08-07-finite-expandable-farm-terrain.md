# 有限可扩展农场地形系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用固定边界、可配置尺寸、分块渲染和可存档的地形系统替换 Main 内 400 个视觉 GroundTile。

**Architecture:** `TerrainMapData` 保有全部地形格子；`TerrainManager` 管理预置场景节点、Chunk、物理、边界、建筑占地和保存。Main 只预置 TerrainManager 和游戏对象，动态创建仅限 Chunk 节点与存档恢复的建筑实例。

**Tech Stack:** Godot 4.5 GDScript、GL Compatibility、ArrayMesh、PackedByteArray、JSON、headless SceneTree 测试。

## Global Constraints

- 静态类型 GDScript；新增代码放在 `scripts/terrain/`。
- 初始宽高 20×20、格子 1.0m、默认 Chunk 10×10；支持至约 100×100。
- 不使用无限地图、流送世界或 GridMap。
- 基础地面碰撞始终为一个 BoxShape3D；格子修改不得重建它。
- 覆盖层只在下一帧按脏 Chunk 批量重建；材质使用 Alpha Scissor 并高于基础地面。
- 保留玩家、相机视觉参数、草丛、GardenHouse、ArtGallery、边界墙和灯光调整。
- ArtGallery 的 `occupies_cells = false`、`persistent = false`；GardenHouse 是严格占地且持久化的建筑。
- Git 提交信息均为 `feat: <中文描述>`。

---

### Task 1: 地形数据模型与坐标测试

**Files:**
- Create: `scripts/terrain/terrain_map_data.gd`
- Create: `tests/terrain/terrain_map_data_test.gd`

**Interfaces:**
- Produces: `TerrainMapData`、`enum CellType { GRASS, DIRT, FARMLAND, PATH, STONE }`。
- Produces: `configure(width: int, height: int, cell_size: float) -> bool`、`get_cell(cell: Vector2i) -> int`、`set_cell(cell: Vector2i, cell_type: int) -> bool`、`is_inside_map(cell: Vector2i) -> bool`、`world_to_cell(world_position: Vector3) -> Vector2i`、`cell_to_world(cell: Vector2i) -> Vector3`。

- [x] **Step 1: 写失败的坐标与边界测试。**

```gdscript
var map := TerrainMapData.new()
map.configure(20, 20, 1.0)
_expect(map.cell_to_world(Vector2i(0, 0)).is_equal_approx(Vector3(-9.5, 0.0, -9.5)), "origin cell center")
_expect(map.world_to_cell(Vector3(9.99, 0.0, 9.99)) == Vector2i(19, 19), "last map cell")
_expect(not map.is_inside_map(Vector2i(-1, 0)), "negative cell is outside")
_expect(not map.set_cell(Vector2i(20, 0), TerrainMapData.CellType.DIRT), "outside writes fail")
```

- [x] **Step 2: 运行测试确认因类不存在而失败。**

Run: `godot --headless --path . -s tests/terrain/terrain_map_data_test.gd`

Expected: non-zero exit with `Could not find type TerrainMapData`.

- [x] **Step 3: 实现紧凑 PackedByteArray 地图及完整边界保护。**

```gdscript
class_name TerrainMapData
extends RefCounted

enum CellType { GRASS, DIRT, FARMLAND, PATH, STONE }

var width: int = 0
var height: int = 0
var cell_size: float = 1.0
var _cells: PackedByteArray = PackedByteArray()

func get_cell(cell: Vector2i) -> int:
    if not is_inside_map(cell):
        return CellType.GRASS
    return _cells[cell.y * width + cell.x]
```

- [x] **Step 4: 运行测试确认通过。**

Run: `godot --headless --path . -s tests/terrain/terrain_map_data_test.gd`

Expected: exit 0 and `TERRAIN_MAP_DATA_TEST_PASS`.

- [x] **Step 5: 提交。**

```bash
git add scripts/terrain/terrain_map_data.gd tests/terrain/terrain_map_data_test.gd
git commit -m "feat: 添加紧凑地形数据模型"
```

### Task 2: 建筑配置、占地规则与测试

**Files:**
- Create: `scripts/terrain/building_footprint.gd`
- Create: `tests/terrain/building_occupancy_test.gd`
- Modify: `scenes/environment/garden_house.tscn`
- Modify: `scenes/environment/art_gallery.tscn`

**Interfaces:**
- Consumes: `TerrainMapData` from Task 1。
- Produces: `BuildingFootprint` with exported `footprint_size: Vector2i`、`anchor_offset: Vector2i`、`placement_offset: Vector3`、`occupies_cells: bool`、`allow_on_farmland: bool`、`persistent: bool`。
- Produces testable Manager APIs: `can_place_building(footprint: BuildingFootprint, anchor_cell: Vector2i) -> bool`、`register_building(building_id: StringName, footprint: BuildingFootprint, anchor_cell: Vector2i) -> bool`、`unregister_building(building_id: StringName) -> void`、`get_building_at_cell(cell: Vector2i) -> StringName`。

- [x] **Step 1: 写失败的 2×2 占地、越界、冲突与 FARMLAND 测试。**

```gdscript
var footprint := BuildingFootprint.new()
footprint.footprint_size = Vector2i(2, 2)
_expect(manager.can_place_building(footprint, Vector2i(3, 3)), "empty footprint can be placed")
_expect(manager.register_building(&"barn", footprint, Vector2i(3, 3)), "registration succeeds")
_expect(not manager.can_place_building(footprint, Vector2i(4, 4)), "overlap is rejected")
_expect(not manager.can_place_building(footprint, Vector2i(19, 19)), "out of bounds is rejected")
```

- [x] **Step 2: 运行测试确认因配置类和管理器 API 不存在而失败。**

Run: `godot --headless --path . -s tests/terrain/building_occupancy_test.gd`

Expected: non-zero exit with missing `BuildingFootprint` or `TerrainManager` API.

- [x] **Step 3: 实现配置组件，并给两个既有预置场景添加显式配置。**

```gdscript
class_name BuildingFootprint
extends Node

@export var footprint_size: Vector2i = Vector2i.ONE
@export var anchor_offset: Vector2i = Vector2i.ZERO
@export var placement_offset: Vector3 = Vector3.ZERO
@export var occupies_cells: bool = true
@export var allow_on_farmland: bool = false
@export var persistent: bool = true
```

GardenHouse 配置为 `Vector2i(3, 3)`、偏移 `Vector2i(-1, -1)`、持久化且占格；ArtGallery 配置为 `occupies_cells = false` 与 `persistent = false`。

- [x] **Step 4: 运行建筑占地测试确认通过。**

Run: `godot --headless --path . -s tests/terrain/building_occupancy_test.gd`

Expected: exit 0 and `BUILDING_OCCUPANCY_TEST_PASS`.

- [x] **Step 5: 提交。**

```bash
git add scripts/terrain/building_footprint.gd scenes/environment/garden_house.tscn scenes/environment/art_gallery.tscn tests/terrain/building_occupancy_test.gd
git commit -m "feat: 添加建筑格子占地配置"
```

### Task 3: TerrainManager、Chunk 覆盖层、物理与边界

**Files:**
- Create: `scripts/terrain/terrain_manager.gd`
- Create: `scenes/terrain/terrain_manager.tscn`
- Create: `tests/terrain/terrain_manager_test.gd`
- Modify: `scenes/main/main.tscn`
- Modify: `tests/smoke_test.gd`
- Delete: `scenes/environment/ground_tile.tscn`

**Interfaces:**
- Consumes: `TerrainMapData` and `BuildingFootprint`。
- Produces: `TerrainManager` with exported `map_width: int = 20`、`map_height: int = 20`、`cell_size: float = 1.0`、`chunk_size: int = 10`。
- Produces: `set_terrain_cell(cell: Vector2i, cell_type: int) -> bool`、`get_autotile_mask(cell: Vector2i) -> int`、`get_dirty_chunk_coords() -> Array[Vector2i]`、`flush_dirty_chunks() -> void`。

- [x] **Step 1: 写失败的自动连接掩码与跨 Chunk 脏标记测试。**

```gdscript
manager.set_terrain_cell(Vector2i(9, 4), TerrainMapData.CellType.DIRT)
manager.set_terrain_cell(Vector2i(10, 4), TerrainMapData.CellType.DIRT)
_expect(manager.get_autotile_mask(Vector2i(9, 4)) == 2, "east neighbor sets bit 2")
_expect(manager.get_dirty_chunk_coords().has(Vector2i(0, 0)), "left chunk is dirty")
_expect(manager.get_dirty_chunk_coords().has(Vector2i(1, 0)), "right chunk is dirty")
```

- [x] **Step 2: 运行测试确认因 TerrainManager 未实现而失败。**

Run: `godot --headless --path . -s tests/terrain/terrain_manager_test.gd`

Expected: non-zero exit with missing `TerrainManager` class.

- [x] **Step 3: 创建预置 TerrainManager 场景和最小实现。**

`terrain_manager.tscn` 预置 `BaseGround`、`GroundBody/CollisionShape3D`、`Chunks`、`Buildings`、`WorldBounds` 及四墙；将原 Main 的 PlaneMesh、BoxShape3D 和边界 Shape 参数迁移进去。Manager 持有唯一地图实例，更新一个 BoxShape3D 与四面墙，并把每个 Chunk 作为唯一可运行时创建的 `MeshInstance3D`。

- [x] **Step 4: 为每个脏 Chunk 构建 ArrayMesh。**

为 DIRT/FARMLAND/PATH/STONE 各创建一个 ArrayMesh surface；每个单元生成四个顶点、六个索引，UV 用 `mask % 4` 与 `mask / 4` 选择 4×4 图集格。预置调试材质设置 `transparency = BaseMaterial3D.TRANSPARENCY_ALPHA_SCISSOR` 和 `cull_mode = BaseMaterial3D.CULL_DISABLED`；顶点 Y 固定 `0.003`。

- [x] **Step 5: 在 Main 迁移场景树。**

删除 `GroundTiles`、400 个实例、`ground_tile.tscn` 引用和旧 Ground/WorldBounds 节点；预置 TerrainManager。将 GardenHouseInstance 与 ArtGalleryInstance 重挂到 `TerrainManager/Buildings`，保持全球变换。更新 smoke test，验证 TerrainManager、单一 GroundBody、WorldBounds 和没有 GroundTiles，同时保留玩家、相机和 ArtGallery 验证。

- [x] **Step 6: 运行 Manager 和 smoke 测试确认通过。**

Run: `godot --headless --path . -s tests/terrain/terrain_manager_test.gd; godot --headless --path . -s tests/smoke_test.gd`

Expected: both exit 0.

- [x] **Step 7: 提交。**

```bash
git add scripts/terrain/terrain_manager.gd scenes/terrain/terrain_manager.tscn scenes/main/main.tscn tests/terrain/terrain_manager_test.gd tests/smoke_test.gd scenes/environment/ground_tile.tscn
git commit -m "feat: 实现分块农场地形渲染"
```

### Task 4: 保存加载、建筑恢复与往返测试

**Files:**
- Create: `tests/terrain/terrain_save_load_test.gd`
- Modify: `scripts/terrain/terrain_map_data.gd`
- Modify: `scripts/terrain/terrain_manager.gd`

**Interfaces:**
- Consumes: Tasks 1–3 的公开接口。
- Produces: `save_to_slot(slot: int) -> bool`、`load_from_slot(slot: int) -> bool`、`has_save_slot(slot: int) -> bool`。

- [x] **Step 1: 写失败的存档往返、缺失文件与无效 JSON 测试。**

```gdscript
source.set_terrain_cell(Vector2i(2, 3), TerrainMapData.CellType.FARMLAND)
_expect(source.save_to_slot(TEST_SLOT), "save succeeds")
_expect(target.load_from_slot(TEST_SLOT), "load succeeds")
_expect(target.map_data.get_cell(Vector2i(2, 3)) == TerrainMapData.CellType.FARMLAND, "terrain round trips")
_expect(not target.load_from_slot(MISSING_SLOT), "missing save is safe")
```

- [x] **Step 2: 运行测试确认因保存 API 缺失而失败。**

Run: `godot --headless --path . -s tests/terrain/terrain_save_load_test.gd`

Expected: non-zero exit with missing save/load implementation.

- [x] **Step 3: 实现版本化 JSON 保存和严格验证。**

保存为 `user://saves/terrain_slot_<slot>.json`，顶层 `version = 1`。序列化地图尺寸、格子值和每个 `persistent` 建筑的 `scene_file_path`、锚点、Y 朝向、占地配置。写入前创建目录并检查文件错误；加载在验证完全通过后才应用新状态。

- [x] **Step 4: 按场景路径重建持久建筑。**

只清理并重建 `persistent` 建筑实例；不删除 ArtGallery。恢复时用 `cell_to_world(anchor) + placement_offset` 设置位置、恢复 Y 角度，随后通过占地 API 注册。场景缺失、配置缺失或占地冲突时安全失败并回滚加载。

- [x] **Step 5: 运行保存测试及完整回归。**

Run: `godot --headless --path . -s tests/terrain/terrain_save_load_test.gd; godot --headless --path . -s tests/terrain/terrain_map_data_test.gd; godot --headless --path . -s tests/terrain/building_occupancy_test.gd; godot --headless --path . -s tests/terrain/terrain_manager_test.gd; godot --headless --path . -s tests/smoke_test.gd; godot --headless --path . -s tests/art_gallery_test.gd`

Expected: every command exits 0 and reports its `*_TEST_PASS` marker.

- [x] **Step 6: 提交。**

```bash
git add scripts/terrain/terrain_map_data.gd scripts/terrain/terrain_manager.gd tests/terrain/terrain_save_load_test.gd
git commit -m "feat: 添加地形与建筑存档加载"
```

### Task 5: 最终审查与交付

**Files:**
- Modify: `docs/superpowers/plans/2026-08-07-finite-expandable-farm-terrain.md`

- [x] **Step 1: 针对新增 GDScript 和场景运行 Godot 代码审查。**

检查所有参数/返回类型、资源预加载、`_process` 中无节点查找、延迟批处理无逐帧重建、节点路径仅指向直接子节点，以及动态节点使用 `queue_free()`。

- [x] **Step 2: 运行最终完整测试组与场景静态加载。**

Run: `godot --headless --path . --editor --quit; godot --headless --path . -s tests/terrain/terrain_map_data_test.gd; godot --headless --path . -s tests/terrain/building_occupancy_test.gd; godot --headless --path . -s tests/terrain/terrain_manager_test.gd; godot --headless --path . -s tests/terrain/terrain_save_load_test.gd; godot --headless --path . -s tests/smoke_test.gd; godot --headless --path . -s tests/art_gallery_test.gd`

Expected: every command exits 0; no parser error、节点缺失或场景引用错误。

- [x] **Step 3: 更新计划进度并提交。**

```bash
git add docs/superpowers/plans/2026-08-07-finite-expandable-farm-terrain.md
git commit -m "feat: 完成有限农场地形实施计划"
```

