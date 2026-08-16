# Godot 联调：INT1 Agent/Backend 收敛记录

> 更新：2026-08-13<br>
> 合同：`@yaya/agent-contracts` v0.4.0<br>
> 状态：原 2026-08-09 P0 缺口已经按单 Gateway 架构收敛；本文是 INT1 历史快照。当前 INT2 已达到 Frontend offline 60/60、Backend 468/468、Agent 601（599 PASS + 2 exact excluded）及受控真实 Provider M2 PASS，详见 [真实 Gateway E2E](../testing/real-gateway-e2e.md)。

## 当前 ownership

- `walnut-world-backend`：唯一公开 Gateway、唯一 PostgreSQL/Alembic 写权（head `017_durable_learner_worker`）、Control/Build/Turn/terminal projection、所有 Product 资源。
- `agent`：v0.4 不可变合同/Ports、provider-neutral Runtime、Build/CAS、Sandbox 与教学库；不作为第二产品后端。
- `walnut-world-frontend`：正式 `app_root.tscn`、Gateway/validator、ClientStore、真实 deadline/retry、HTTP Events/Snapshot 与 Product Interaction 恢复。

## 原 P0 缺口处置

| 原编号 | 当前处置 | 证据边界 |
|---|---|---|
| C-01/C-02 | 已锁定追加式 v0.4 release descriptor 并在 Backend/Frontend pin；远端 annotated tag 已发布 | 138 files、26,127 bytes、manifest SHA-256 `b62a6152f1f2fd87d1941beecd3a1d47089811e91b067a8b225ff0d7a5ce72b9`；tag 指向 `0494c0f8ef6eb505e43db84c0249b046be35c589`；v0.3 release lock 不变 |
| C-03/C-04 | Student Bootstrap v0.4 提供 Session create request、Build policy、Activation scope/registry revision 与 exact active tuple | 客户端不猜 revision、profile、compiler/test/capability |
| B-01 | ContentUnit、SessionWorkspace、Draft GET/PUT、Interaction 已由唯一 Backend 装配 | Session Control 原子创建 starter Draft/Workspace；Draft/Turn/terminal projection 刷新 Workspace |
| B-02 | Build worker 使用完整 source bundle、pinned Docker、PUBLIC/HIDDEN tests、Artifact CAS 与 Build-terminal Certification | 失败 Build 零 Artifact/SkillVersion/Certification/Evidence |
| B-03 | full-scope Activation registry CAS 与 immutable GET 已装配 | exact version/artifact/certification/scope/revision 闭合 |
| B-04/A-02/A-04 | combined worker 闭合 Turn → Run/World/Event/Evidence → Learner/Interaction/Workspace | Provider 失败保留客观事实，但无 fallback Interaction/Learner advance |
| Frontend runtime | AppRoot 不接收人工 Session ID；ClientStore 持久化 exact tuple/envelope；Run 后恢复 HTTP Events/Snapshot/Evidence/Interaction；启动先恢复 pending Draft/Turn | offline headless 46/46、0 failure/skip；nullable hint、跨 Session cursor、跨 ClientStore Draft/Turn response-loss、真实 Draft PUT/CAS、scene-tree lifecycle 与正式 UI display 回归通过；两条 real-Provider opt-in 用例为 `EXCLUDED_NOT_RUN` |

## INT1 真实验收与剩余边界（历史）

27.6 秒 direct-POST 与 169.836 秒 fixture recoverable-relay 诊断继续只保留为历史证据。INT1 当时唯一真实 Provider cross-process E2E 在 194.12 秒取得 **`REAL_PROVIDER_PRIVATE_DURABLE_RELAY` PASS**：DeepSeek V4 Flash 为 `source=provider`、`degraded=false`；4 个 Turn/Run/Interaction/Learner projection、2 个 Build/Certification/Activation、9 个 terminal Command、11 个 Evidence、8 个 Sandbox receipt、2 个 Artifact 文件闭合；13 unique dispatch / 13 generation 且单 dispatch 最大 1。Provider response-loss 恢复同一 dispatch；Gateway/workflow/learner 新 PID 后 recovery-only 为 8 GET / 0 mutation，relay/database/Sandbox/Artifact/response-loss proxy 五类指纹不变。Windows 运行使用 digest-pinned host Docker，不是 production private DinD live；公开 Gateway pending write response-loss 仍为 `NOT_PROVEN`。远端 `refs/tags/agent-contracts-v0.4.0` 已发布并指向 `0494c0f8ef6eb505e43db84c0249b046be35c589`，且通过 manifest 校验。

显式 billable wrapper 已从 production HS256 authority seeder 的允许前置权威和空 Artifact root 开始执行 `docs/testing/real-gateway-e2e.md` 的正式 runner，并记录完整 identity/revision/sequence/hash 与副作用指纹。普通 offline discovery 中两条 opt-in 用例仍为 `EXCLUDED_NOT_RUN`，但该测试选择状态不否定独立 live PASS。

## 明确排除

- WSS/Client Event Batch：Backend production 默认不挂载，v0.4 `StudentBootstrapV2` 不包含对应 capability 或 stream 响应字段，本轮只使用 HTTP Events/Snapshot；
- Skill Patch/PatchDecision 主链：production 默认不注册 dormant PatchDecision router，三项 eligibility 保持 false；
- Feishu、Client Event Batch 新纵切、自动 Patch/Build/Activation、空账号/内容创作与 UI 美术重做。

原 C-05/C-06、B-05、A-03 属于这些后续/排除阶段，不阻断 INT1 HTTP 学生闭环，也不得被宣称为本轮交付。
