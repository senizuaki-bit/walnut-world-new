# ADR：INT1 单一 Gateway 与组件所有权

- 状态：Accepted
- 日期：2026-08-13
- 性质：对已经落地的 INT1 拓扑作正式现状追认；不声称本 ADR 在实现前已经存在

## 背景

历史 Agent A6/A8 composition 包含可独立启动的 `yaya_agent_backend`、私有 `yaya_*` 表和迁移，适合保留单仓兼容与回归证据，但不能与 Walnut 产品后端同时成为 Godot 的生产权威。INT1 需要一个公开入口、一套业务写入与迁移权威，并继续复用 Agent 已验证的合同、Runtime、Build、Sandbox 和教学能力。

## 决策

生产拓扑固定为：正式 Godot AppRoot 只访问 `walnut-world-backend`；Backend 独占公开 HTTP、产品 PostgreSQL/Alembic、durable job/receipt、业务事务和投影；Agent 只发布不可变 Wire 合同、Ports 与 provider-neutral 库。

| 边界 | 唯一 owner | 决策 |
|---|---|---|
| 面向 Godot 的公开 HTTP Gateway | `walnut-world-backend` | 不代理或启动第二个 `yaya_agent_backend` HTTP 服务 |
| 产品数据库 Schema、写入事务和迁移链 | `walnut-world-backend` | 只迁移 Backend-owned 表；不读取或双写 Agent 私有 `yaya_*` 表 |
| Control / Build / Turn / terminal projection durable worker | `walnut-world-backend` | 一个 combined workflow worker 装配生产状态机、lease/fencing 与 receipt 对账 |
| Learner durable worker | `walnut-world-backend` | 消费 Backend-owned projection job，并在 Backend 事务边界推进 Learner |
| Wire Schema 与版本治理 | `agent/contracts` | v0.3 字节冻结；v0.4 追加发布并由 Manifest、消费者描述符和 release ref 锁定 |
| Runtime、Build、Sandbox、Teaching 能力 | `agent` provider-neutral libraries | 通过稳定 Ports/包边界复用；不拥有产品 HTTP、ORM、迁移或业务表 |
| Provider relay、Docker 与 Artifact/workspace 的生产装配 | `walnut-world-backend` | 只装配 capability-verified recoverable relay、digest-pinned Docker 与持久根目录 |
| 正式学生流程、恢复与展示 | `walnut-world-frontend` | AppRoot/ClientStore 只通过公开合同访问 Gateway，不直连 Provider、Docker 或数据库 |

## 强制约束

- 禁止 Gateway 代理 Agent 历史 HTTP 服务、两套业务数据库双写、Backend 读取 `yaya_*` 私表，以及用同步脚本、复制实现或人工 SQL 伪装一致性。
- `yaya_agent_backend` 只保留历史 A6/A8 回归用途；不得作为 INT1 production service 或运行其私有 migration。
- Build/Sandbox 生产路径不得回退宿主编译器或原生执行；Provider 生产路径不得回退普通 direct chat adapter。
- 合同变化只能发布新的追加式版本；不得修改 v0.3 冻结字节。三仓消费者必须验证同一 Manifest、文件 hash 和 release identity。
- Skill Patch/PatchDecision 主链、WSS、Client Event Batch 与 Feishu 继续排除；存在冻结合同不等于 production route 已挂载。

## 结果与验证

该决策让 Session、Draft、Build/Certification、Activation、exact-version Turn、Run/World/Event/Snapshot/Evidence、Learner、Interaction 与 Workspace 共享同一 Backend authority。Agent 的历史单仓 live、fixture relay 和 deterministic local harness 只能证明各自声明的证据层，不能替代真实 Provider 三仓验收。

当前实现、门禁和仍未关闭的 live 风险以 [INT1 三仓验证报告](INT1_CROSS_REPOSITORY_VALIDATION_REPORT.md) 为准。Agent 库边界见 [Runtime](AGENT_RUNTIME.md)、[Build](AGENT_BUILD_LIBRARY.md) 与 [Sandbox](AGENT_SANDBOX_LIBRARY.md)；合同发布身份见 [`contracts/manifest.json`](../contracts/manifest.json)。
