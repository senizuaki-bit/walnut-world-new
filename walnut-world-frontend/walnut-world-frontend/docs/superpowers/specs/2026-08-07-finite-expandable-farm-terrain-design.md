# 有限可扩展农场地形系统设计

## 目标

为 Godot 4.5、GL Compatibility 的正交俯视 3D 农场实现固定边界、可配置尺寸的格子地形。默认地图为 20×20 格、每格 1m，设计上可安全扩展至约 100×100；不使用无限地图、流送或 GridMap。

## 已确认的迁移决定

- 删除 Main 中仅用于视觉表现的 `GroundTiles` 及其 400 个 `GroundTile` 实例和场景依赖。
- 保留并迁移既有基础草地平面、整块地面碰撞、`WorldBounds` 墙、相机、玩家、草丛、GardenHouse 与 ArtGallery，以及用户未提交的灯光与美术变更。
- GardenHouse 是严格占地建筑。
- ArtGallery 是覆盖多处地块的 30 个展示摆件集合：挂载同一 Inspector 占地配置接口，但默认 `occupies_cells = false`，不参与占地冲突检测，也不在存档恢复时作为可放置建筑重建。该决定避免它与 GardenHouse 的既有视觉布局冲突。

## 场景树

```text
Main
├── TerrainManager (Node3D, 预置)
│   ├── BaseGround (MeshInstance3D)
│   ├── GroundBody (StaticBody3D)
│   │   └── CollisionShape3D
│   ├── Chunks (Node3D)
│   ├── Buildings (Node3D)
│   └── WorldBounds (Node3D)
│       ├── LeftWall / RightWall / TopWall / BottomWall
│       └── 各自的 CollisionShape3D
├── CameraRig
├── GrassInstances
├── GardenHouseInstance
├── ArtGalleryInstance
└── Player
```

`TerrainManager` 是地形行为、渲染批处理、地面和边界尺寸同步、建筑占地注册及存档入口；`TerrainMapData` 只保存地形值，不访问场景树。预置节点承载固定组成，运行时仅创建 Chunk 网格节点和存档恢复的建筑实例。

## 数据与坐标

`TerrainMapData` 是唯一地形数据源，使用 `PackedByteArray` 保存以下值：`GRASS`、`DIRT`、`FARMLAND`、`PATH`、`STONE`。它公开带边界保护的 `get_cell`、`set_cell`、`is_inside_map`、`world_to_cell`、`cell_to_world`。

地图原点位于中心；格子 `(0, 0)` 的中心位于 `(-width / 2 + 0.5, 0, -height / 2 + 0.5)`。转换只使用 X/Z，格子世界尺寸由配置决定。

## 渲染与物理

`BaseGround` 是始终存在的一张草地 `PlaneMesh`。非草地格子由 10×10 默认 Chunk 的 `ArrayMesh` 覆盖层绘制，每个格子按照四方向同类型邻居产生 0–15 自动连接掩码，并以该掩码选择 4×4 图集 UV。

覆盖材质使用 Alpha Scissor，网格放在基础草地上方的微小固定高度，避免透明排序成本与 Z-fighting。正式的 16 格透明边缘图集尚未交付时，使用命名为临时调试可视化的纯色材质；它不是最终美术资源。

修改一个格子时，只将该格所属 Chunk 及可能受连接变化影响的四个相邻 Chunk 标为脏；通过 `call_deferred` 在下一帧一次性批量重建。没有脏 Chunk 时没有重建操作。

地面物理始终只有一个 `BoxShape3D`，尺寸根据地图宽高和厚度更新；切换格子不会重建碰撞。四面边界墙在地图尺寸应用时同步移动和更新其 BoxShape3D。

## 建筑占地

`BuildingFootprint` 是附着在可放置建筑根节点的 Inspector 配置组件，保存 `footprint_size: Vector2i`、`anchor_offset: Vector2i`、`occupies_cells: bool` 与 `allow_on_farmland: bool`。不从模型碰撞或节点路径推断占地。

`TerrainManager` 集中提供 `can_place_building`、`register_building`、`unregister_building`、`get_building_at_cell` 和 `place_building`。校验完整占地在地图内、没有已有占用，并且默认拒绝 FARMLAND。占用索引仅保存稳定建筑 ID 与锚点格，不保存节点引用。

GardenHouse 和 ArtGallery 都使用组件配置接入：前者登记严格占地；后者仅暴露配置且不登记占用。

## 存档格式与迁移

存档放在 `user://saves/terrain_slot_<slot>.json`，使用 JSON，不使用 Resource 序列化。首版格式：

```json
{
  "version": 1,
  "terrain": { "width": 20, "height": 20, "cell_size": 1.0, "cells": [0] },
  "buildings": [
    {
      "scene_path": "res://scenes/environment/garden_house.tscn",
      "anchor_cell": { "x": 12, "y": 8 },
      "rotation_y": 0.0,
      "footprint": { "size": { "x": 3, "y": 3 }, "offset": { "x": -1, "y": -1 } }
    }
  ]
}
```

加载前验证 JSON 类型、版本、地图尺寸、格子数量与值域；不存在或无效存档返回失败结果并保持当前运行状态。加载成功后应用地图尺寸、复制地形数组、重建全部 Chunk，并清理及按场景路径重新实例化持久建筑。节点引用绝不进入保存文件。版本迁移由逐版本函数处理。

## 测试与验收

沿用项目的 headless `SceneTree` 测试风格，为下列公开行为新增自动测试：

1. 格子/世界坐标双向转换及边界。
2. 自动连接掩码及跨 Chunk 边界的脏 Chunk 标记。
3. 多格建筑的占地、冲突、越界和 FARMLAND 拒绝。
4. 存档—加载往返，包括无存档和无效存档。

同时更新 smoke test：验证 TerrainManager 及其预置节点存在、没有 `GroundTiles`、地面和边界仍可用，并继续覆盖现有玩家、相机、GardenHouse 与 ArtGallery 验证。
