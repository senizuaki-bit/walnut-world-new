# main 后端固定版本的本地前端联调

## 当前结论

2026-09-10 真人试用修正：本机四声道麦克风触发 Godot WASAPI 不支持声道数的错误，原生采集无有效输入，导致没有转写。前端已增加本机可选的 FFmpeg 单声道采集兼容方案，并让连接/聆听/失败状态立即显示在对话框。真实麦克风采集及关闭释放通过，完整问题界面的真实豆包回复显示和结束后保留通过。详见 [麦克风兼容记录](windows-microphone-compatibility.md)；此前合成音频测试不等于真人硬件验收。

2026-09-10 更新：前端正式界面到真实 Gateway、PostgreSQL、Docker 编译沙箱、Worker 和 DeepSeek 的正常业务链路已验证。用户提供临时凭据后，真实 Godot 语音客户端→main Gateway→豆包实时语音也已通过两轮合成语音输入验收；实际麦克风、耳机听感尚待人工检查。

- 集成分支：`codex/frontend-main-local-20260909`，工作树 `D:/FeishuAIreview/walnut-interface-audit-20260909`。
- 基线：`origin/main` / `00052e9`。后端、Agent 和根目录文档均保留 main；所有提交差异限定在 `walnut-world-frontend/`。
- 原开发目录与 `fronted-art` 分支保持原样。只做本地提交，不推送。
- 前端运行版本为 Godot 4.7.1；main 根 README 仍描述 4.5.2，启动时显式传入 4.7.1。前端版本测试检查实际引擎及前端自有入口，不要求修改 main 文档。

## 已验证

| 范围 | 实际结果 |
| --- | --- |
| 服务启动 | Docker Engine 29.7.2；PostgreSQL、Gateway、Relay、Worker、Learner Worker、游戏均运行 |
| 正常主链路 | AppRoot 启动→真实文字提问→界面 RunButton 触发编译/激活/运行→世界提交→模型反馈成功 |
| 错误辅导 | 连续三次真实编译失败，世界未变化；三次得到非降级 Provider 回复，角色依次 teaching_agent、teaching_agent、bug_agent |
| 修正后运行 | 正确代码成功；世界 revision 从 4 到 5，Run 为 `run_e55067c8203d0406aae14bbe` |
| 成功后连续提问 | 两次请求均正常闭环，最终 Interaction sequence 16，未留下 agent_hint 恢复请求 |
| 进程重启 | Stop/Start 后新前端进程恢复相同 Session、active Skill、Draft hash/revision、World hash/revision、Interaction cursor；HTTP 写请求为 0 |
| 离线回归 | 完整 84 项批次中 83 通过，唯一失败是测试要求只读 main 根 README 更新版本；改为检查实际引擎及前端自身入口后，该项单独重跑通过。未把旧批次日志改写为全绿 |
| 工坊动画断言 | 原失败把验证错误区的有意排版变化算成动画移动；现在等错误区布局完成后测按钮动画稳定性，保留独立错误布局测试，两项均通过 |
| 语音客户端协议 | 前轮真实 Godot WebSocket→main 路由测试通过；外部 Provider 与上下文读取被测试替身替换，此证据不代表真实豆包语音通过 |
| 语音取消与失败边界 | 新增用例复现空 response_id 取消后旧字幕/音频继续进入的问题；前端修复后通过。配置失败会释放连接与音频、恢复提问按钮，代码入口仍可用 |
| 真实豆包凭据 | main Agent 原有检查脚本返回 VOICE_CONNECTED、VOICE_AUDIO_OK，收到 153644 字节的 24 kHz PCM |
| 真实三端语音 | 使用生产 Godot 客户端、真实 main 路由和豆包；以本机合成的 16 kHz PCM 替代物理麦克风，两轮均收到识别文字、回复文字和音频并进入播放；上下文从测试数字 17 更新为 83 后正确回答 83；打断收到服务端确认，关闭释放播放资源 |
| 语音结束事件 | 实测部分回答只有 response.output_audio.done，没有 response.done；前端支持两种结束事件并去重，保留已排队音频。回归用例修复前失败、修复后通过，取消后的匿名结束事件不会误报新回答完成 |
| 语音后数据恢复 | 只读检查确认 Session、Skill、Draft、World、Interaction cursor 与此前指纹一致，零 HTTP 写请求 |

正常主链路在现有本地持久样例数据中执行，新增了测试草稿、编译失败、Run 和聊天记录，没有重置这份数据库。完成时已恢复为成功代码。数据卷被保留。

## 未完成与限制

1. **真人语音与凭据有效期：** 2026-09-09 的 `VOICE_CONFIGURATION_INVALID` 已由临时进程环境配置解除。供应商凭据未写入代码、配置文件、Git 或前端进程；用户计划测试后销毁，销毁后语音将不可用，后续服务重启也需要重新提供有效凭据。自动验收使用合成录音和无头音频驱动，不能证明物理麦克风权限、实际扬声器听感或回声效果。实际真人连续说话、插话和耳机体验仍待检查。
2. **故障注入：** main 自带真实 Provider 验收在独立临时数据库中故意丢失一次模型响应，编译后的提示工作流报 `hint decision closure mismatch: SOURCE,DEGRADED`，终态 INTERNAL_ERROR。本轮未修改后端/Agent，也没有通过前端伪造反馈掩盖错误。正常网络路径随后独立验证通过。
3. **可选世界演出：** 当前 Crop 场景沿用发布的 `world_presentation_enabled=false`、`skill_patch_enabled=false`。强制打开另一条 HARVEST 演出开关时，历史持久数据接口返回 EVENT_SEQUENCE_GAP；没有为通过验收而跳过校验或把本地动作冒充权威演出。
4. 部分 Godot 场景短时退出仍报告 ObjectDB/resource 未释放；不影响已验证业务断言，但不能称为无警告。

## 复现

在组合工作树根目录启动现有服务，显式指定实际引擎及 Python。后端 `.venv` 是到原目录依赖环境的本地 Junction，源码加载由脚本的 PYTHONPATH 指向本工作树 main 版本。

```powershell
$env:PATH = 'D:/Docker/resources/bin;' + $env:PATH
$godot = (Get-Command Godot_v4.7.1-stable_win64_console.exe).Source
& ./walnut-world-backend/scripts/start-persistent-play.ps1 -Action Start `
  -PythonExe 'D:/FeishuAIreview/walnut-world-new/walnut-world-backend/.venv/Scripts/python.exe' `
  -GodotExe $godot
```

真实业务检查会提交测试代码及产生模型调用，需使用本地测试账号/数据：

```powershell
& ./walnut-world-frontend/scripts/testing/run-local-alignment-check.ps1 -CurrentSessionSmoke
```

重启验证先用 `-CurrentSessionSmoke -SnapshotOnly -FingerprintPath <绝对文件路径>` 保存只读指纹，重启现有服务后再用 `-CurrentSessionSmoke -RecoveryOnly -FingerprintPath <同一路径>` 验证。凭据从受限本地运行目录读取，JWT 仅保留于进程环境，不打印或保存。

## 本机证据

- `audit-results/live-current-session-full.log`
- `audit-results/live-recovery-before.json`、`live-recovery-after.log`
- `audit-results/live-voice-readiness.json`
- `audit-results/live-doubao-frontend-after-fix.log`（真实供应商；前述 readiness 是未配置时的历史结果）
- `audit-results/voice-audio-done-before-fix.log`、`voice-audio-done-after-fix.log`
- `audit-results/live-after-doubao-recovery.log`
- `audit-results/live-m1-e2e.log`（故障注入失败证据）
- `audit-results/final-frontend-regression.log`、`final-version-check.log`
