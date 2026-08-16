# Godot 前端：INT1 外部依赖状态（历史快照）

> 更新：2026-08-13。这里保留 INT1 外部状态，不执行 `git stash`，也不隐藏或丢弃任何工作区改动。当前 INT2 已闭合 deterministic actual10 与受控真实 Provider M2；现行证据见 [真实 Gateway E2E](../testing/real-gateway-e2e.md)，不要把下表的 INT1 排除项解释为当前 INT2 缺口。

| 编号 | 能力 | 当前事实 | 状态 |
|---|---|---|---|
| EXT-01 | Student Bootstrap → Session → Content/Workspace/Draft | 唯一 Backend 已装配；Session Control 创建 starter Draft/Workspace；AppRoot 无人工 Session ID | 已解决 |
| EXT-02 | Build → Certification → Activation | Backend durable worker 使用 pinned Docker、PUBLIC/HIDDEN tests、Artifact CAS 与 full-scope Registry CAS；真实 live 闭合 2 个 Build/Certification/Activation | 已解决并通过 live |
| EXT-03 | Turn → Run/World/Event/Evidence/Learner/Interaction/Workspace | Backend terminal projection 与 Frontend exact closure 已在四轮真实 live 闭合 | 已解决并通过 live |
| EXT-04 | 真实 Provider 跨进程验收 | 194.12 秒 `REAL_PROVIDER_PRIVATE_DURABLE_RELAY` PASS；DeepSeek V4 Flash 为 `source=provider`、`degraded=false`；13/13 generation 且单 dispatch 最大 1 | **已解决 / PASS** |
| EXT-05 | WSS / Client Event Batch | INT1 明确排除；Backend production 默认不挂载，v0.4 `StudentBootstrapV2` 不包含对应 capability 或 stream 响应字段，HTTP Events/Snapshot 是本轮恢复权威 | 后续阶段 |
| EXT-06 | Skill Patch/PatchDecision | INT1 明确排除；production 默认不注册 dormant PatchDecision router；`allow_skill_patch=false`、`patch_eligible=false`、`full_solution_eligible=false` | 后续阶段 |
| EXT-07 | Feishu | INT1 明确排除 | 后续阶段 |

## Provider 验收结果

受控 live 从 production HS256 authority seeder 允许的 Published Content、初始 World、Learner/Profile、BuildPolicy、LaunchAuthority、revision-zero Registry 和空 Artifact root 开始，正式 `app_root.tscn` 经唯一 Gateway、fresh PostgreSQL、durable workers 与私有 recoverable relay 调用 DeepSeek V4 Flash。Secret 未写入仓库、fixture、trace、日志或报告。

2026-08-12 的 27.6 秒 direct-POST 与 169.836 秒 fixture recoverable-relay 结果继续只保留为历史证据。INT1 当时的独立 real-Provider live 闭合 4 个 Turn/Run/Interaction/Learner projection、9 个 terminal Command、11 个 Evidence、8 个 Sandbox receipt、2 个 Artifact 文件；Provider response-loss 恢复同一 dispatch，三服务新 PID 后 8 GET / 0 mutation，五类指纹不变。该 Windows 结果是 digest-pinned host-Docker live，不是 production private DinD live；公开 Gateway pending write response-loss 仍为 `NOT_PROVEN`。

## 未被阻断的前端工作

- v0.4 138-file release drift gate；
- AppRoot/Gateway/Validator/ClientStore offline regression；
- stable operation envelope、真实 deadline/backoff/`Retry-After`；
- HTTP Events/Snapshot gap recovery、Evidence/Interaction closure；
- 预置场景和只基于已验证资源的展示。
