# 核桃代码世界 SFX

2026-09-09 下载并接入的首批 CC0 音频，共 9 个文件，约 0.7 MB。短音效为 44.1 kHz／单声道／16 位 WAV，环境声为 OGG。文件已做峰值限制及首尾淡入淡出；这些是技术处理，不代表人工听感验收已完成。

| 文件 | 原始素材 | 使用位置 |
|---|---|---|
| click.wav | Kenney Interface Sounds / click_001.ogg | 开始页与作物关卡原生按钮点击 |
| panel_open.wav | 同包 / open_001.ogg | 打开代码抽屉 |
| panel_close.wav | 同包 / close_001.ogg | 关闭代码抽屉 |
| confirm.wav | 同包 / confirmation_001.ogg | 正式构建通过、教学实验正确反馈 |
| retry.wav | 同包 / error_004.ogg | 构建失败、验证失败、实验纠错、链路异常 |
| activate.wav | 同包 / confirmation_004.ogg | 正式技能激活通过 |
| complete.wav | Kenney Music Jingles / Pizzicato jingles / jingles_PIZZI03.ogg | 正式闭环完成；独立教学模式完成 |
| watering.wav | rubberduck / loop_water_02.ogg 前 3 秒 | 可见 WATER 演出；动画结束、跳过、隐藏关卡时停止 |
| birds.ogg | isaiah658 / birds-isaiah658_0.ogg 前 24 秒 | 低音量农场环境声；代码、对话及阅读面板开启时静音 |

## 来源与许可

- [Kenney Interface Sounds](https://kenney.nl/assets/interface-sounds)，CC0；原许可文件见 [licenses/interface-License.txt](licenses/interface-License.txt)。
- [Kenney Music Jingles](https://kenney.nl/assets/music-jingles)，CC0；原许可文件见 [licenses/jingles-License.txt](licenses/jingles-License.txt)。
- [40 CC0 water / splash / slime SFX](https://opengameart.org/content/40-cc0-water-splash-slime-sfx)，作者 rubberduck，发布页标注 CC0；见 [来源记录](licenses/water-source.txt)。
- [Ambient Bird Sounds](https://opengameart.org/content/ambient-bird-sounds)，作者 isaiah658，发布页标注 CC0；见 [来源记录](licenses/birds-source.txt)。
- [CC0 1.0 官方说明](https://creativecommons.org/publicdomain/zero/1.0/)允许复制、修改、分发和商用；建议保留这些来源记录。

[sources.json](sources.json) 记录原包下载链接、原包及所选原文件 SHA-256、处理滤镜、成品 SHA-256。原始压缩包和全部解压素材在本机仓库根目录 `tools/sfx-source-cache/`，该缓存不进入 Git；运行游戏只需本目录文件。没有采用前轮需署名的 CC BY 素材。

## 播放与调整

所有播放器预置在 `res://scenes/audio/farm_audio.tscn`，分别挂在开始页与作物关卡；场景隐藏会停止其全部声音。音量在播放器的 `volume_db` 调整，总线在 `res://default_bus_layout.tres` 中配置为 UI、SFX、Ambience → Master。鸟鸣循环开关保存在 `birds.ogg.import`。

正式 Build/Activation 只监听 `CropAgentBridge` 完成信号。通关提示跟随原有完成入口，候选成功、历史恢复、动画结束都不触发正式通关音。音频不写入 ClientStore，不影响世界判定或教学进度。同一音效 100 毫秒内限播一次，避免连续事件叠加；声音结束不驱动任何业务状态。

鸟鸣片段首尾各有 1 秒淡入淡出，循环包含自然的音量起伏。浇水使用固定音色随可见动画起止，1／2 份水和倍速的听感差异来自播放时长；未做音高随倍速升高处理。

## 验证入口

- `res://tests/level_demo/farm_audio_test.gd`：资源、总线、循环、安静编辑、候选与正式结果区分、重复状态、跳过、隐藏关卡。
- `crop_agent_bridge_test.gd`：真实控制器接口的替身驱动 Build→Activation→Run 提示链及重复绑定清理。
- `crop_candidate_bridge_test.gd`：候选 WATER 有水声、结束即停止、没有正式完成音。
- `scripts/run-offline-tests.ps1`：完整离线前端回归。
