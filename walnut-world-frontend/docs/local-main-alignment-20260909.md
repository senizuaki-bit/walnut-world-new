# main 后端固定版本的本地前端联调

## 当前结论

2026-09-09，前端正式界面到真实 Gateway、PostgreSQL、Docker 编译沙箱、Worker 和 DeepSeek 的正常业务链路已验证。实时语音仍被后端凭据配置阻塞，尚不能宣布所有功能验收完成。

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

正常主链路在现有本地持久样例数据中执行，新增了测试草稿、编译失败、Run 和聊天记录，没有重置这份数据库。完成时已恢复为成功代码。数据卷被保留。

## 未完成与限制

1. **真实语音：** 实际 main WebSocket 在 start 后返回 `voice.error / VOICE_CONFIGURATION_INVALID`。默认 Agent 密钥文件不存在，当前进程环境未配置豆包凭据；等待用户提供本机密钥路径。密钥只配置后端进程环境，不加入代码/提交，也不发给前端。之后仍需完成真实语音连通和耳机/麦克风验收。

   后续复核了原目录/集成目录默认密钥路径，以及 Process、User、Machine 三层豆包凭据环境变量，均未配置。当前主服务仍为 RUNNING。前端按主协议将无 ID 的取消作用于当前回答，并过滤在途旧包；鉴权错误提示同时涵盖游戏与上游凭据问题，不再一律误报游戏登录过期。
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
- `audit-results/live-m1-e2e.log`（故障注入失败证据）
- `audit-results/final-frontend-regression.log`、`final-version-check.log`
