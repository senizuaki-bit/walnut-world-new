# 关卡 Demo 前端资源目录

更新时间：2026-08-10

## 用途与边界

本目录记录 `walnut-world-frontend` 可复用美术、Godot 预置场景，以及《横向自动浇水器》Demo 的资源映射结果。

当前生成资源共 39 张 PNG，39 份 `.png.import` 均已存在并应随源码提交：角色 8、作物 6、设施 17、地块 4、UI 2、特效序列 2。Godot 导入缓存继续只保存在被忽略的 `.godot/` 中。

## 角色资源（8）

以下资源均为透明背景正面角色图，已在 `scenes/environment/art_gallery.tscn` 中以预置 `Node3D + Sprite3D` 节点验证：

| 资源 | 路径 | 预置节点 |
|---|---|---|
| 小探险家 | `assets/art/generated/characters/little_explorer.png` | `ArtGallery/LittleExplorer` |
| 芽芽 | `assets/art/generated/characters/yaya_sprout.png` | `ArtGallery/YayaSprout` |
| 小核桃 | `assets/art/generated/characters/little_walnut.png` | `ArtGallery/LittleWalnut` |
| 叮当 | `assets/art/generated/characters/ding_dang.png` | `ArtGallery/DingDang` |
| 叮当大师 | `assets/art/generated/characters/master_ding_dang.png` | `ArtGallery/MasterDingDang` |
| 书书 | `assets/art/generated/characters/shu_shu.png` | `ArtGallery/ShuShu` |
| 害虫 | `assets/art/generated/characters/pest_bug.png` | `ArtGallery/PestBug` |
| 杂草芽 | `assets/art/generated/characters/weed_sprout.png` | `ArtGallery/WeedSprout` |

另有可移动玩家场景 `scenes/player/player.tscn`，使用四方向待机、行走图集：

- `assets/art/characters/player_boy_idle_4dir.png`
- `assets/art/characters/player_boy_walk_4dir.png`

## 作物资源（6）

| 资源 | 路径 | 预置节点 |
|---|---|---|
| 胡萝卜 | `assets/art/generated/crops/carrot.png` | `ArtGallery/Carrot` |
| 番茄 | `assets/art/generated/crops/tomato.png` | `ArtGallery/Tomato` |
| 草莓 | `assets/art/generated/crops/strawberry.png` | `ArtGallery/Strawberry` |
| 南瓜 | `assets/art/generated/crops/pumpkin.png` | `ArtGallery/Pumpkin` |
| 果树幼苗 | `assets/art/generated/crops/fruit_tree_sapling.png` | `ArtGallery/FruitTreeSapling` |
| 新种幼苗 | `assets/art/generated/crops/young_seedling.png` | `WateringPlot/Seedling` |

## 设施资源（17）

| 资源 | 路径 | 预置节点 |
|---|---|---|
| 种子站柜台 | `assets/art/generated/facilities/seed_station_counter.png` | `ArtGallery/SeedStationCounter` |
| 农场市场摊位 | `assets/art/generated/facilities/farm_market_stall.png` | `ArtGallery/FarmMarketStall` |
| 农场气象站 | `assets/art/generated/facilities/farm_weather_station.png` | `ArtGallery/FarmWeatherStation` |
| 自动浇水车 | `assets/art/generated/facilities/automatic_watering_cart.png` | `ArtGallery/AutomaticWateringCart` |
| 农场水泵站 | `assets/art/generated/facilities/farm_water_pump_station.png` | `ArtGallery/FarmWaterPumpStation` |
| 播种机 | `assets/art/generated/facilities/seed_planter_machine.png` | `ArtGallery/SeedPlanterMachine` |
| 农场巡逻无人机 | `assets/art/generated/facilities/farm_patrol_drone.png` | `ArtGallery/FarmPatrolDrone` |
| 果园果树 | `assets/art/generated/facilities/orchard_fruit_tree.png` | `ArtGallery/OrchardFruitTree` |
| 蓝图制作台 | `assets/art/generated/facilities/blueprint_crafting_bench.png` | `ArtGallery/BlueprintCraftingBench` |
| 温馨档案图书馆 | `assets/art/generated/facilities/cozy_archive_library.png` | `ArtGallery/CozyArchiveLibrary` |
| 农场仓库 | `assets/art/generated/facilities/farm_warehouse.png` | `ArtGallery/FarmWarehouse` |
| 订单配送站 | `assets/art/generated/facilities/order_delivery_station.png` | `ArtGallery/OrderDeliveryStation` |
| 温室 | `assets/art/generated/facilities/greenhouse.png` | `ArtGallery/Greenhouse` |
| 温室控制锁 | `assets/art/generated/facilities/greenhouse_control_lock.png` | `ArtGallery/GreenhouseControlLock` |
| 古代魔法树 | `assets/art/generated/facilities/ancient_magic_tree.png` | `ArtGallery/AncientMagicTree` |
| 算法试炼石 | `assets/art/generated/facilities/algorithm_trial_stone.png` | `ArtGallery/AlgorithmTrialStone` |
| 星光传送门 | `assets/art/generated/facilities/starlight_portal.png` | `ArtGallery/StarlightPortal` |

项目另有独立、可直接实例化的环境预置场景：

- `scenes/environment/garden_house.tscn` → `assets/art/environment/garden_house.png`
- `scenes/environment/grass_patch.tscn` → `assets/art/environment/grass_patch.png`

`art_gallery.tscn` 是一次性资源验收展台，不应直接作为正式关卡设施集合实例化。关卡文档确定所需设施后，应在关卡场景中预置对应的 `Node3D + Sprite3D`，或把需要复用两次以上的设施拆成独立 `.tscn`。

## 地块资源（4）

全部由 `scenes/terrain/terrain_manager.tscn` 以预置 `StandardMaterial3D` 使用：

| 状态 | 路径 | 现有接入点 |
|---|---|---|
| 草地 | `assets/art/generated/terrain/grass_plot.png` | `GroundMaterial` / `BaseGround` |
| 翻耕土地 | `assets/art/generated/terrain/tilled_soil.png` | `DebugDirtMaterial` |
| 浇水土地 | `assets/art/generated/terrain/watered_soil.png` | `DebugFarmlandMaterial` |
| 土路 | `assets/art/generated/terrain/dirt_road.png` | `DebugPathMaterial` |

`TerrainManager` 已提供有限 20×20 地面、碰撞边界、`Chunks`、`Buildings` 和地块材质列表。关卡 Demo 优先实例化此场景，不重新创建地形脚本。

## UI 资源（2）

| 资源 | 路径 | 现有接入点 |
|---|---|---|
| 种子任务签 | `assets/art/generated/ui/task_seed_banner.png` | `TaskWorkspace/Hud/SafeArea/EdgeLayer/TaskTag/Artwork` |
| 树叶对话框边框 | `assets/art/generated/ui/leaf_dialogue_frame.png` | `StoryDialogueOverlay/DialogueCard/LeafFrame` |

其导入配置为 Lossless、无 mipmap、启用 Fix Alpha Border，符合固定尺寸 UI 纹理要求。

## 特效序列资源（2）

| 资源 | 路径 | SpriteFrames / 动画 |
|---|---|---|
| 水壶浇水 4×2 序列 | `assets/art/generated/effects/watering_can_sequence.png` | `resources/sprites/watering_can_frames.tres` / `pour` |
| 书书施法 4×2 序列 | `assets/art/generated/effects/shu_shu_magic_sequence.png` | `resources/sprites/shu_shu_magic_frames.tres` / `cast_loop_water` |

## 可直接复用的组合场景

| 场景 | 责任 | 关卡 Demo 用法 |
|---|---|---|
| `scenes/terrain/terrain_manager.tscn` | 有限农场地形、地块材质、碰撞与建筑容器 | 作为关卡地形根实例 |
| `scenes/player/player.tscn` | 玩家移动与四方向动画 | 文档包含可控角色时实例化 |
| `scenes/environment/garden_house.tscn` | 花园小屋设施 | 文档需要主屋时实例化 |
| `scenes/environment/grass_patch.tscn` | 草丛装饰 | 用作边缘装饰，不承载交互 |
| `scenes/task/task_workspace.tscn` | 世界优先 HUD、任务签、代码抽屉 | 需要编程界面时作为上层工作区 |
| `scenes/main/main.tscn` | 当前主世界组合样例 | 仅作布局与灯光参考，不直接当关卡文档实现 |
| `scenes/level_demo/watering_plot.tscn` | 单块可点击土地、干湿表面、幼苗、编号 | 横向浇水关卡复用十个实例 |
| `scenes/ui/story_dialogue_overlay.tscn` | 灰色遮罩、居中角色、树叶边框、打字机与全屏继续 | 可复用的儿童剧情对话层 |
| `scenes/level_demo/horizontal_watering_demo.tscn` | 两排土地、教学角色、手动/魔法水壶与精简 HUD | 可直接运行的 frontend-only Demo |

## 缺失资源判定与生图流程

收到关卡文档后，逐项建立“文档名词 → 本目录资源”映射：

1. 名称和功能均能对应：直接复用，不生图。
2. 功能相同但外观有文档特征：优先编辑现有资源的非破坏性版本，不覆盖原图。
3. 角色、作物、设施或 UI 语义确实不存在：使用内置 Image Gen，引用 `little_walnut.png`、`garden_house.png`、`seed_station_counter.png` 中最相关的 1–3 张作为风格参考。
4. 透明素材使用纯 `#ff00ff` 色键、无投影的单一对象图，再通过 `remove_chroma_key.py` 生成 alpha PNG。
5. 最终文件进入对应的 `assets/art/generated/<category>/`；Godot 生成的 `.png.import` 一并提交，`.godot/imported/` 不提交。
6. UI/小型 2D 素材使用 Lossless、无 mipmap、Fix Alpha Border；3D 远景纹理是否启用 mipmap由实际使用方式决定。

## 《横向自动浇水器》映射结论

- 直接复用：芽芽、小核桃、叮当、Bug、书书、草地、翻耕土地、浇水土地。
- 语义缺失并已生图：新种幼苗、手动/魔法共用水壶序列、书书施法序列、树叶对话边框。
- 已移除旧五喷头横向装置；自动阶段改为“书书施法 → 大水壶按 0—4 逐块浇灌”，与手动阶段形成明确视觉差异。
- 不直接实例化 `TerrainManager`：该 Demo 需要两排固定编号土地与独立点击区，使用复用纹理构成更小的预置 `WateringPlot` 子场景；没有重复创建地形管理逻辑。
- 关卡方案已归档到 `docs/level-demo/横向自动浇水器_Demo关卡方案_精简版_v1.0.md`，当前无资源阻塞项。
