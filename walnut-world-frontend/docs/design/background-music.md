# 背景音乐接入

## 问题

将负责人提供的 Morning_In_The_Garden_Patch.mp3 接入 Godot 前端，要求循环播放及淡入淡出。

## 结论

GameFlow 预置唯一背景音乐播放器，开始页与关卡持续共享。原 MP3 不转码，导入启用 loop；默认 -20 dB，启动和正常关闭窗口分别有 1.2 秒淡入、淡出。

## 关键理由

音乐放在页面共同父节点，切页和重开不会重复叠加或重置进度。独立 Music 总线便于与 UI、音效、鸟鸣分别调节。保留用户原文件，避免重复有损编码。

## 做出的决定

仅修改 Godot 前端，预置节点优先；不修改后端、Agent，不推送。正常关闭窗口等待音乐淡出结束；场景释放时还原退出设置。音频来源为用户提供，不标为 CC0。

## 后续行动

运行项目（F5）试听整体音量。如需调整，在 background_music.tscn 的 music_volume_db 和 fade_seconds 修改。独立关卡 F6 不会装配 GameFlow 的 BGM。

## 未解决问题

自动检查覆盖循环、渐变及页面连续性，实际扬声器听感待负责人体验。

## 关联项目/笔记

- 项目：D:/FeishuAIreview/walnut-world-new/walnut-world-frontend
- 场景：scenes/audio/background_music.tscn
- 播放与来源：assets/audio/README.md
- 测试：tests/level_demo/background_music_test.gd
