# 作物关卡 V2 美术与动态接入

## 问题

把《作物适配浇水器》V2.2 静态美术与动态 V1 接到新版本 Godot 前端，在独立工作树验证，不推送远端。

## 结论

- 基线：`82900ab`，`codex/bug-legion-production-chain`。
- 新工作树：`D:/FeishuAIreview/walnut-world-art-v2`。
- 新分支：`codex/frontend-art-v2-animation`，仅本地提交。
- 正式入口：`walnut-world-frontend/project.godot` → `scenes/app/app_root.tscn`，使用 Godot 4.7.1。
- 单独查看作物关卡：运行 `scenes/level_demo/crop_adaptive_watering_demo.tscn`。这是已有本地教学路径，正式连接仍使用 AppRoot。

## 关键理由

静态包的整页原型和网页演示用于对照，实际游戏使用独立素材与原生控件。动画包提供的是表现资源，不能用动画结束推导正式世界提交、成功、湿度或技能解锁。

## 做出的决定

- 100 张可接入静态 PNG 放在 `assets/art/redesign/crop_adaptive/v2/components/`。三个废弃矩形高亮框没有导入。
- 动态包保留 PNG 图集、poster、98 份元数据及六个 OGV 环境背景；不复制备选 WebP/MP4/WebM、生成脚本和原型大图进入运行资源目录。
- 已接入首页、2×4 农田、工坊两步填空与汇总、C++ 卷轴、角色对话、五节点技能树及三能力卡、Bug 挑战、成长档案、工具展示；全局按钮和输入状态由 Theme 控制。
- 环境使用预置 VideoStreamPlayer；角色、作物、土地和反馈使用预置 TextureRect 与图集播放器。图集后台加载，等待期间显示 poster，过期加载结果不能覆盖当前角色或状态；隐藏时冻结并释放图集，退出时回收未完成加载。
- 说话角色在打字结束后切回 idle；土地四态消费现有数据或候选结果；选中、焦点和错误使用土地轮廓动画。浇水器依现有动作播放约 2 秒／4 秒两种单次动画，保留原有时钟、倍速与回执边界。
- “减少动态”在当前运行会话内生效。环境暂停、纹理显示 poster，浇水器显示静态帧但动作时钟仍完成。长对话采用原生滚动容器。
- 保留六个 LineEdit、CodeEdit、节点唯一名、信号、草稿、存档、候选与正式运行区分。未改 Agent、后端或发布合同；正式 WATER 表现开关保持关闭。
- UI 图片使用 Godot 导入缩小与九宫格适配，源 PNG 字节不改动。布局以当前 1280×720 视口适配；没有直接复制 1672×941 的网页坐标。

## 后续行动

- 使用新工作树打开项目，检查个人审美偏好及真实账号下的画面；此分支尚未合并或推送。
- 资源重建命令：`python walnut-world-frontend/scripts/import_art_v2.py`，生成 Theme、StyleBoxTexture 和浇水 SpriteFrames；场景本身已预置并可在编辑器中修改。
- 若以后导出 PCK，导出配置须包含动态读取的 `assets/art/redesign/crop_adaptive/v2/motion/metadata/*.json`、图集、poster 和所用 OGV。此次只做本地开发，没有新增平台导出配置。

## 验证与未解决问题

- Godot 4.7.1 完整离线测试：79/79 通过；后续长对话滚动与静态浇水回退的定向复测通过。
- 真实网关两项 opt-in 测试未执行；本地 HTTP AppRoot E2E、正式/候选隔离、角色队列和 Bug 军团测试通过。
- 实际渲染截图见 `verification/art-v2/`，覆盖首页、农田、技能可学习/已解锁、工坊、对话、卷轴、Bug、成长和完成反馈。截图来自本地教学状态，不是新的正式服务回执。
- 98 段资源均保留元数据，当前控制器没有触发的扩展姿态仍为备用资源；没有为播放这些素材新增玩法或解锁条件。
- 尚未执行远端推送、合并、平台导出或完整真实网关复验。

## 关联项目/笔记

- `README.md`：正式前端运行和协议边界。
- `assets/art/redesign/crop_adaptive/v2/reference/`：原始接入指南、布局、技能数据、动画映射。
- `tests/level_demo/art_v2_presentation_test.gd`：动画、轮廓、只读投影、角色切换、隐藏与减少动态回归。

## 中文字体与阅读调整（2026-09-06）

- 按用户反馈提高文字清晰度。采用 GitHub `notofonts/noto-cjk` 的 Noto Sans SC Medium（500）与 Bold（700），代码编辑器使用 Noto Sans Mono CJK SC Regular。字体原件、OFL 1.1 许可、固定上游提交与 SHA-256 位于 `assets/fonts/noto-sans-sc/`。
- 1280×720 下，农田数值从 12px 提升为 17px，辅助文字 17–18px，主要正文/输入/代码 20px，对话正文 24px，标题依层级 22–44px。标题、按钮和强调使用真实粗体。
- 浅底使用深色正文，深色名牌保留浅色文字；农田数值/缺口/结果有不透明浅色底板。移除正文厚描边与投影；结果颜色直接设置字体色，避免整体调制把底板和深色字一起染暗。
- LineEdit 焦点样式使用透明中心、3px 边框，避免焦点层遮住文字；占位、选中、不可编辑状态均有明确颜色。代码抽屉加宽，保留原生滚动和编辑功能；技能解锁说明按内容撑开。
- 保留所有节点唯一名、原生输入、信号及玩法数据。现有场景回归测试显式使用 1280×720 视口，并继续检查说明面板不超过 110px、农田和底部操作不重叠。
- 验证：Godot 4.7.1 离线测试最终 79/79 通过，无引擎错误/警告；真实网关两项 opt-in 未执行。三份字体均覆盖当前相关场景与脚本中的 467 个汉字；代码字体 ASCII 字符等宽。11 个实际渲染状态截图及最终测试摘要见 `verification/typography/`。
- 用户可从 `scenes/app/game_flow.tscn` 查看本地教学预览；正式服务入口仍为 AppRoot。
