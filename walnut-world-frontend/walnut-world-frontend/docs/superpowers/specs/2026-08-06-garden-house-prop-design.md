# 花园小屋摆件接入设计

## 目标

将用户提供的手绘花园小屋作为透明广告牌摆件接入 Main 场景，并与既有 `grass_patch` 保持同一世界比例与落地基线。

## 资源处理

- 输入：`D:\Chrome\ChromeDownloads\ChatGPT Image 2026年8月6日 21_42_56.png`，`1254×1254`。
- 输出：`assets/art/environment/garden_house.png`，`1254×1254`、RGBA PNG。
- 洋红背景转换为透明 alpha；保留建筑本体、白色描边和所有装饰。
- 将有效建筑像素在输出画布中水平居中、底边对齐至底部安全边距，消除原图大面积下方留白造成的悬空。
- 输出四角必须完全透明，并检查边缘没有洋红溢色。

## 场景接入

- 新增 `scenes/environment/garden_house.tscn`，根节点为 `Node3D`。
- 子节点为 `Sprite3D` 广告牌，使用 `billboard = 1`、`transparent = true`、`shaded = false`、`pixel_size = 0.0016`，与 `grass_patch.tscn` 一致。
- 精灵中心高度为 `Y=1.0`，使完整 `1254px` 画布的底边落在地面 `Y=0` 附近。
- Main 新增一个预置实例，放在可活动区域内且不与现有草丛重叠；不增加碰撞或运行时生成逻辑。

## 验收

1. 新图为 `1254×1254` 的透明 PNG，四角 alpha 为 0，建筑边缘无明显洋红晕边。
2. 建筑在 Main 中可见、始终面向相机、底部贴地，不浮空也不穿入地面。
3. 建筑使用与 `grass_patch` 相同的广告牌缩放和材质响应方式。
4. Godot 4.5.2 可重新导入主场景；既有测试仅报告与本次资源无关的既存失败。
