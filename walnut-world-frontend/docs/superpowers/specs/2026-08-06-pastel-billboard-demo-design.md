# Pastel Billboard Demo 设计

## 目标

在 Godot 4.5.2 中制作一个可立即运行的最小 3D 世界演示：一个四方向移动的 2D 广告牌角色、一个平面地面、可复用草丛和固定方位的弱透视跟随相机。

## 架构

- `main.tscn` 组合环境、地面、边界、相机、草丛和玩家实例。
- `player.tscn` 以 `CharacterBody3D` 为根，视觉和运动职责分离。
- `grass_patch.tscn` 是唯一草丛来源，主场景只重复实例化它。
- Image Gen 精灵表通过预定义 Atlas/`SpriteFrames` 资源接入，不在运行时创建场景节点。

## 取舍

选择低 FOV 的透视相机而非正交投影，以保留轻微空间纵深；选择预置 `SpriteFrames` 而非运行时拆分图集，以便资源可在编辑器中检查并满足预置节点优先原则。
