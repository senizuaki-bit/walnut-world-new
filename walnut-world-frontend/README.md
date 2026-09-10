# 核桃代码世界前端

本仓库承载核桃代码世界的正式 Godot 学生端。`scenes/app/app_root.tscn` 是 INT1/INT2 composition root：它只接收 Gateway base URL 与短期 Bearer token，不接受人工 Session ID；通过 Student Bootstrap 创建/恢复 Session，再恢复 Content、Workspace、Draft、exact active tuple、Snapshot、presentation 与 Interaction。

## 运行

使用 Godot 4.7.1 stable 打开 `project.godot`，运行 `scenes/app/app_root.tscn`。`project.godot` 的 `config/features` 固定为 `4.7`；测试和正式启动脚本统一校验 4.7.1。学生端只访问唯一 `walnut-world-backend` Gateway；不直接调用模型、Docker、数据库、本地编译器或 sibling Agent HTTP。

## 作物关卡美术 V2

当前分支已接入静态 V2.2 与动态 V1，包含绘本场景、角色状态、土地轮廓、技能树和原生输入皮肤。布局与动画仍消费原控制器状态；详细范围、运行入口、验证与导出注意事项见 [接入说明](docs/design/art-v2-integration.md)。实际渲染截图见 [验证目录](docs/design/verification/art-v2/)。

## 角色 2D 提示与权威世界演出

`all` 的正式入口已接入每局一次的 Bug 挑战：主关成功后生成变式题，在独立编辑器中判题，通过后获取书书总结及完整配音。旧自动 Bug/Book 交互仍同步游标，但不再展示。网络恢复、验证结果与真实配音待办见[联调记录](docs/testing/bug-practice-integration.md)。下述旧角色提示行为保留于未启用新练习的兼容入口。

叮当师傅新增[长按提问前端原型](docs/design/mentor-question-prototype.md)，用于本地演示聆听、逐字回复和“我懂了”关闭流程；不读取麦克风或请求语音/Agent 服务，示例回复不代表真实 Agent 输出。固定台词沿用原对话框。

这两条表现链彼此独立，不能混用：

- `AgentInteraction.role=bug_agent` 经 `AgentInteractionPresenter` 的 FIFO 队列触发本地 `world_cue_requested("bug_legion", active)`，只显示/关闭 `BugLegion2D`。这是角色对话的本地 2D 视觉提示，不创建 `WorldPresentationEvent`，不推进 World revision，也不改变 Snapshot。
- 权威世界演出由已提交 Run 的 `WorldPresentationEvent`、Receipt、Event high watermark 和最终 Snapshot 闭包驱动。正式作物关卡当前仍设置 `world_presentation_enabled=false`，因为已发布协议尚未提供 WATER 权威演出；不能为了 Bug 军团打开该开关。

正式角色反馈统一复用 `StoryDialogueOverlay`。关卡叙事占用 Overlay 时，Agent Interaction 按 `interaction_id` 去重并排队；叙事结束后按 sequence/FIFO 顺序继续展示。

## 测试

```powershell
.\scripts\run-offline-tests.ps1 -GodotExe D:\Godot\godot.cmd
```

脚本自动发现离线用例，并排除两条显式 opt-in 的真实网关测试；实际通过数以脚本输出的 `OFFLINE_TEST_SUMMARY` 为准。

## 接口边界

Wire合同唯一权威为sibling `../agent/contracts/manifest.json`及其引用文件。当前Frontend descriptor指向additive v0.6 candidate：148 entries、28,042-byte manifest、SHA-256 `bb4f1c12f34125bcc4b79b6a1330a20848a7f43764d87fa7c8d7668e7994fe14`；v0.4/v0.5历史字节继续锁定，v0.6 tag尚不存在，release identity为`NOT_PROVEN`。前端不得使用旧`/api/*`、猜authority或本地编译C++。

AppRoot 的产品链是 `Student Bootstrap → Session + starter Draft/Workspace → Draft CAS → Build/Certification → Activation → exact-version Turn → Run/World receipt/Evidence → HTTP Events/Snapshot → Learner/Product Interaction → recovery/display`。Command polling 使用真实 deadline、退避和 `Retry-After`；ClientStore 持久保存 exact tuple 与响应丢失 envelope。启动时若存在 pending Turn，AppRoot 必须在 READY 前以原 request/key 和 Turn 前 cursor 对账；闭包前不会按恢复后的 Workspace 高水位生成新 Turn identity。

三仓当前通过数不在 README 中硬编码，以各自完整门禁的机器可读 summary 为准。历史 deterministic actual10 与受控真实 Provider M2 均已 PASS：4 次客观失败后，学生通过 Request Patch 打开预览并在 Dialog 显式 `ACCEPT`，随后手动 Build、Activate 和 Run。真实 Provider 运行 `run868a` 用时 301.012 秒，DeepSeek `deepseek-v4-flash` 为 `source=provider`、`degraded=false`，18 unique dispatch / 18 generation、单 dispatch 最大 1；Provider relay response-loss 恢复同一 dispatch 且 generation 仍为 1。Patch 达到 `PUBLIC_UI_CHAIN_CLOSED`；Phase 1 为 12 POST/1 PUT、6 Turn/5 Run/11 terminal Command（7 `APPLIED` + 4 `REJECTED`）、1 条 World commit 与 8 条 presentation，Phase 2 为 17 GET/0 mutation，并恢复同一权威指纹。公开 Gateway pending write response-loss（不同于上述 Provider relay response-loss）仍为 `NOT_PROVEN`。

INT2 Patch入口只有在本地World/Patch flags与Backend capability全为true时才可用；Backend mutation route默认关闭并条件挂载。任一层false时入口不可用且零Patch POST。WSS、Client Event Batch、Feishu、自动应用/Build/Activate/Run与多文件Patch明确排除。
