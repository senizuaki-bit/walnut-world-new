# Agent Turn 故障恢复

> INT1 ownership（2026-08-12）：以下不变量继续适用，但实现 owner 已迁到 sibling `walnut-world-backend` 的 durable jobs/receipts/fencing。当前 strict Provider 恢复只重放已持久化 receipt；缺最终 Provider 结果时保留 Run/World/Evidence 并 fail loud，不发布 fallback Interaction、不推进 Learner。

## 1. 接受响应丢失

HTTP 只有在 Session sequence、Command、Job、原始请求字节和 202 receipt 全部提交后才发送 202。若 commit 已确认但响应序列化/传输状态未知，返回 `503 UNKNOWN_COMMIT_STATE`、原 `command_id` 和精确 `Location`。

客户端必须以相同 `Idempotency-Key` 和完全相同的 UTF-8 请求字节重试。服务返回首次 receipt，`Idempotency-Replayed: true`；不同字节返回 `IDEMPOTENCY_KEY_REUSED`，不得执行第二次。

## 2. Worker 崩溃与 lease

Job 领取使用 PostgreSQL clock、`FOR UPDATE SKIP LOCKED`、随机 `lease_id` 和 `worker_id`。heartbeat、状态迁移、完成与 retry 都同时校验 live lease 和 fencing token。旧 Worker 在 lease 过期或接管后对 Job/Command/World/AgentTurn 的写入必须为零行。

Worker 重启后会依次对账已提交 AgentTurn 和 invocation receipt；已有结果时只恢复 Command/DONE，不再调用模型或 Sandbox。临时数据库/依赖错误把 Job 恢复为 READY；确定的身份、合同或 authority 损坏会一次终结为 FAILED/DONE，避免 poison job 活锁。

## 3. Sandbox 与 World 提交状态

Sandbox 只产出 typed ActionIntent，不能声明任务成功，也不能直接写 World。WorldEngine 在内存中重算 proposal；生产 World UoW 以旧 revision 和 state hash 做 CAS，并在同一事务写 World、1 条聚合 `world.committed` Event 和对应的 1 条 projection Outbox。8 个已应用 intent 的 ID 全部保存在该事件的 `applied_intent_ids` 中。

SkillInvocation 的同一外层事务还写 Evidence、Run 和以 `invocation_id` 为键的 receipt：

- 明确 statement/serialization 失败且 PostgreSQL 确认 rollback：返回 `ROLLED_BACK`，同 invocation 可重新运行；World 不变。
- COMMIT 响应丢失：先查询 receipt。可见则恢复精确结果；数据库暂不可查询则报告 `UNKNOWN_COMMIT_STATE`，不得写 fallback 或另造 invocation。
- receipt 已存在：返回原 Run，禁止再次运行 C++ 或再次 CAS。
- 旧 revision：返回 World conflict，最多一个并发提交者成功。

7/8、Sandbox 失败和恶意自报成功会持久化失败 Run/Evidence/receipt，但不会推进 World revision/sequence。8/8 才允许 revision `n -> n+1`。

## 4. AgentTurnCommit

AgentTurn commit 在同一事务写 committed record、`agent.turn.feedback_ready` Event、feedback/product 两条 projection Outbox、Message、Product AgentInteraction，并给 Run 写入同一 feedback/Evidence set。任一写点失败会完整 rollback，原 claim 仍可由同一 fencing token恢复。

如果 AgentTurn 已提交而 Command/DONE 尚未提交，重启 Worker 从 committed record 恢复 Command 终态，不重新进入 Runtime。无 Run 的明确 provider fallback 只能终结为 `APPLIED/NO_EFFECT`；已经发出副作用但 receipt 未知时不能使用该路径。

## 5. Outbox

Outbox 以 `tenant + destination + idempotency_key` 和 payload SHA-256 去重。发送者通过 lease 领取；旧 lease 不能 mark sent/retry。状态为：

```text
PENDING -> SENDING -> SENT
                  -> RETRYING -> SENDING
                  -> DEAD_LETTER
```

运维处理 RETRYING/DEAD_LETTER 时必须保留原 payload、hash 和 idempotency key，不得手工复制成新消息。Product projection Outbox 的 `agent_feedback_events`、`product_agent_interactions` 以及每条 `world_events` 均可与权威 Event/Interaction 逐字节和 SHA-256 对账。

## 6. PostgreSQL 中断

连接或事务中断时不要手工把 Command 标记 UNKNOWN/FAILED。恢复 PostgreSQL 后重启 Worker；它会从 receipt/AgentTurn/lease 状态继续。开放事务在数据库重启后由 PostgreSQL 回滚，测试要求 Command/Job 或 World/Run/Evidence 整组为零或整组可见，不能部分可见。

建议告警：READY 最老年龄、LEASED 过期数、UNKNOWN_COMMIT_STATE 次数、Outbox RETRYING/DEAD_LETTER、World/Event sequence 漂移、receipt reconciliation 失败和 Sandbox 容器残留。

## 7. 对账清单

一次成功 watering turn 应满足：

- Command `APPLIED/WORLD_COMMIT`，链接到唯一 Run；
- Run `SUCCEEDED`，Sandbox `SUCCEEDED`，8 个 ActionIntent；
- Evidence 恰含 `SANDBOX_LOG` 与 `WORLD_COMMIT`，hash 按 canonical JSON 重算一致；
- World revision 增加 1，event sequence 连续增加 1，snapshot hash 可重算；
- 1 条 `world.committed` Event 含 8 个唯一 `applied_intent_ids`，并有同 payload/hash 的 Outbox；
- 1 条 feedback Event、2 条 Agent projection Outbox、1 条 Message、1 条 Product AgentInteraction；
- Run feedback 与 AgentTurn decision 的 session/turn/command/run/Evidence set 完全相同；
- 相同 HTTP 请求重放后以上计数、World 和 model-request trace 全部不变。

任何一项不一致都按 durable invariant 故障处理，停止自动重试并保留审计证据，禁止通过手工更新 JSON 绕过约束。
