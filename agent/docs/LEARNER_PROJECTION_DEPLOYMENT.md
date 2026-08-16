# Learner Projection 生产部署

> INT1 说明（2026-08-12）：本文是 Agent 单仓历史投影部署记录。当前生产 Learner projection 由 sibling `walnut-world-backend` combined workflow worker 在 Backend-owned tables/UoW 中完成；不得启动独立 `yaya_agent_backend learner-worker` 形成第二数据库权威。

## 1. 进程与数据流

Learner Projection 是 Agent Turn 之后的独立、持久化投影进程。Agent Turn 的提交事务负责写入 `learner.inference.recorded`、对应 Outbox、`yaya_learner_projection_jobs` 及最终 committed fence；Learner Worker 不调用模型，也不重新执行 Sandbox。它只把已经提交且身份、hash、Evidence 均闭合的推断投影为下一轮可读取的 Learner Model。

```text
Agent Turn transaction
  -> learner:<learner_id> / learner.inference.recorded
  -> yaya_learner_projection_jobs (READY)
  -> learner projection Outbox

python -m yaya_agent_backend learner-worker
  -> DB-clock claim + heartbeat + fencing
  -> LearnerPort.project(expected_learner_revision)
  -> snapshot revision +1 + source receipt
  -> learner-model:<learner_id> / learner.model.updated + Outbox
  -> Job SUCCEEDED
```

`learner:<learner_id>` 是只含不可变 inference 输入的源流；`learner.model.updated` 和 `learner.projection.failed` 写入 `learner-model:<learner_id>` 派生流。禁止把派生事件写回源流，否则源 checkpoint 将不再表示连续的 inference sequence。投影只接受两类闭合事实：绑定到真实 Run/Skill 的 `SANDBOX_LOG` 或 `WORLD_COMMIT` Evidence，以及绑定到失败 CompileResult/Skill 的 `TEST_REPORT` Evidence；两类事实都必须逐字节闭合 actor、content、session、turn、command/build、版本、时间和 payload hash。

生产建议至少把以下进程部署为独立故障域：

- HTTP：认证、严格 Wire 校验、原子受理与查询；
- Agent Turn Worker：模型、工具与 Agent Turn commit；
- Learner Projection Worker：有序消费 inference；
- Outbox sender：至少一次投递派生消息；
- PostgreSQL：所有 lease、receipt、snapshot、Event 和 Outbox 的唯一持久化事实源。

多个 Learner Worker 可以并行运行；同一 learner 的 job 仍按源 sequence 串行。每个实例必须使用唯一 worker ID。

## 2. Migration 与发布顺序

`0002_learner_projection.sql` 在 `0001_agent_turn.sql` 之后执行，新增：

- `yaya_learner_models.request_context_json`、`projection_policy_version` 与 `snapshot_sha256`，并约束 provenance 完整时 `revision = projected_through_sequence`；
- `yaya_learner_projection_jobs`；
- `yaya_learner_projection_job_evidence`；
- `yaya_learner_projection_receipts`；
- `yaya_learner_projection_failures`；
- `yaya_learner_projection_terminal_audits` 及终态事务触发器；
- Event、Evidence、Session 和 Learner Model 所需的 authority/hash 复合约束及领取索引。

推荐发布顺序：

1. 备份 PostgreSQL，并确认可以恢复到发布前一致性点。
2. 部署同时理解旧数据和 `0002` 的新应用版本，但暂不切换生产流量。
3. 只运行一次 migration：

   ```powershell
   python -m yaya_agent_backend migrate
   ```

   独立 `migrate` 命令属于完整 Agent 生产配置，按 `docs/AGENT_TURN_DEPLOYMENT.md` 提供全部必需变量。依赖最小化的 `learner-worker` composition 也会在启动时使用同一 advisory lock 自动执行 migration；只部署该进程时只需它自己的数据库与 contracts 配置。

4. 核对 `yaya_schema_migrations` 同时包含 `0001_agent_turn.sql` 和 `0002_learner_projection.sql`。
5. 启动 Learner Worker，先观察 canary tenant/learner 的投影与重放。
6. 启动 Agent Turn Worker 和 HTTP，逐步恢复流量。

`PostgresDatabase.migrate()` 用 advisory lock 串行化 migration，并保存每个 SQL 文件的 SHA-256。已经应用的 migration 是不可变发布物；hash 漂移必须阻止启动，禁止修改 `0002` 后覆盖原文件。后续 schema 变化只能新增递增 migration。

可用以下只读查询确认 migration：

```sql
SELECT name, sha256, applied_at
FROM yaya_schema_migrations
ORDER BY name;
```

## 3. 配置

`learner-worker` 使用依赖最小化的独立 composition，不构造 HTTP、认证、LLM、Sandbox、artifact store 或 Agent Runtime。它只要求以下共享配置：

| 变量 | 说明 |
|---|---|
| `YAYA_DATABASE_DSN` | PostgreSQL DSN；账号需要 learner migration、job、receipt、snapshot、Event 和 Outbox 权限 |
| `YAYA_CONTRACTS_ROOT` | 含受校验 `manifest.json` 的绝对 contracts 目录 |

Worker 自身配置：

| 变量 | 默认值 | 约束与说明 |
|---|---:|---|
| `YAYA_LEARNER_WORKER_ID` | `learner_worker_0001` | 1—128 字符；每个并发实例必须唯一 |
| `YAYA_LEARNER_WORKER_LEASE_SECONDS` | `30` | 2—3600 秒；使用 PostgreSQL 时钟 |
| `YAYA_LEARNER_WORKER_POLL_MS` | `100` | 10—60000 ms；无可领取 job 时的轮询间隔 |

可恢复错误的内部 retry delay 固定为 1 秒，不暴露环境变量。不要通过把 lease 设置得极短来获得更快重试；lease 必须覆盖一次投影事务和正常数据库抖动。部署平台的优雅终止窗口应大于 lease，并为当前投影事务留出完成时间。

`YAYA_LLM_*`、`YAYA_ARTIFACT_ROOT`、`YAYA_SANDBOX_*`、HTTP 和认证变量都不是该进程的依赖。`learner-worker` 不访问 LLM Provider、C++ artifact 或 Sandbox；这些组件也不得被投影失败重新调用。Agent Turn/HTTP 进程仍分别按 `docs/AGENT_TURN_DEPLOYMENT.md` 配置。

## 4. 启动、扩容与优雅停止

先 migration，再为每个实例设置唯一 ID 并启动独立进程：

```powershell
$env:YAYA_DATABASE_DSN = "postgresql://<service-account>@<host>/<database>"
$env:YAYA_CONTRACTS_ROOT = (Resolve-Path .\contracts)
$env:YAYA_LEARNER_WORKER_ID = "learner_worker_node01_0001"
$env:YAYA_LEARNER_WORKER_LEASE_SECONDS = "30"
$env:YAYA_LEARNER_WORKER_POLL_MS = "100"
python -m yaya_agent_backend learner-worker
```

扩容只需增加使用不同 `YAYA_LEARNER_WORKER_ID` 的相同进程。Job 通过 `FOR UPDATE SKIP LOCKED` 领取；claim、heartbeat、成功、retry 和失败终结都必须同时校验 `worker_id + lease_id + fencing_token` 以及数据库时钟下仍未过期的 lease。

使用 Ctrl-C、SIGINT 或 SIGTERM 停止。CLI 会设置 stop event，停止领取新 job，并让当前 `run_once` 收敛后退出。编排平台应先把实例标记为 terminating，再发送终止信号；不要先删除数据库连接，也不要手工清空 lease 字段。

如果进程被强制杀死，job 保持 `LEASED` 直到数据库时钟判定过期。新 Worker 随后以更大的 fencing token takeover；旧进程即使恢复，也不能 heartbeat、提交 snapshot、写 receipt 或改变 job。禁止通过 SQL 提前把未过期 lease 改成 READY。

## 5. 上线验收

先提交一条教学 canary，再等待 Learner Worker。一次成功投影应同时满足：

- 源 `learner.inference.recorded` 的 stream 是 `learner:<learner_id>`，sequence 连续；
- job 从 `READY -> LEASED -> SUCCEEDED`，`attempt == fencing_token`；
- snapshot 的 `revision = previous_revision + 1`，`projected_through_sequence` 等于源 sequence；
- `(tenant_id, event_id)` 只有一条 source receipt；
- receipt 的 event/inference hash 与 job 一致；
- `learner.model.updated` 位于 `learner-model:<learner_id>`，并有一条 hash 一致的 Outbox；
- 对应 terminal audit 已在完整只读对账后写入 `verified_at`；
- 下一 Agent Turn 的 ContextBuilder 读取新 revision、stage、assistance、review time 和当前任务相关 Evidence；
- 重放同一 Agent Turn 或 source event 后，上述计数和 Learner revision 全部不变。

`learner_projection_v1` 的 snapshot 是有界工作集：按 source stream 的确定顺序保留最近 64 个不可变 EvidenceRef，并把每个 competency 的 `evidence_ids` 过滤到该集合；失去最后一条保留证据的陈旧 competency 会被确定性移除。完整历史仍由 source Event、Job Evidence 和 receipt 保存。在线投影与 rebuild 使用同一压缩规则，因此第 65 条及后续合法 inference 不会阻塞 learner stream。

基础运行状态查询：

```sql
SELECT state, count(*) AS jobs,
       min(created_at) AS oldest_created_at
FROM yaya_learner_projection_jobs
GROUP BY state
ORDER BY state;

SELECT tenant_id, learner_id, revision, projected_through_sequence,
       projection_policy_version, snapshot_sha256, updated_at
FROM yaya_learner_models
ORDER BY updated_at DESC
LIMIT 50;

SELECT tenant_id, job_id, terminal_kind, attempt, fencing_token,
       terminal_at, verified_at, verified_by
FROM yaya_learner_projection_terminal_audits
WHERE verified_at IS NULL
ORDER BY terminal_at, tenant_id, job_id;

SELECT j.tenant_id, j.learner_id, j.source_stream_sequence,
       j.state, r.learner_revision, r.model_updated_event_id,
       r.outbox_message_id
FROM yaya_learner_projection_jobs AS j
LEFT JOIN yaya_learner_projection_receipts AS r
  ON r.tenant_id = j.tenant_id AND r.job_id = j.job_id
ORDER BY j.created_at DESC
LIMIT 50;
```

建议告警至少覆盖：

- READY 最老年龄和每个 learner 的 backlog；
- 已超过 `lease_expires_at` 的 LEASED 数量；
- retryable failure 速率与 attempt 增长；
- 未解决的 PERMANENT/QUARANTINED failure；
- `UNKNOWN_COMMIT_STATE` 与 receipt reconciliation 失败；
- 未验证 terminal audit 的数量与最老年龄；任何 `MISSING`/`CORRUPT` audit 都必须触发 durable-invariant incident；
- source sequence 缺口、snapshot checkpoint 超前或 receipt 缺失；
- learner projection Outbox 的 RETRYING/DEAD_LETTER。

## 6. 升级与回滚

普通二进制升级按以下顺序：停止产生新教学 turn，优雅停止 Learner Worker，确认没有未过期 LEASED job，备份，运行新增 migration，启动 Learner Worker，做 canary，最后恢复 Agent Turn/HTTP 流量。强制中断时不需要也不允许清理 lease；等待 takeover 即可。

回滚应用前先确认旧版本理解已经应用的 schema、事件版本和 projection policy version。不能理解新 inference/event/policy 的旧版本不得启动。数据库 migration 不做破坏性 down migration；需要恢复时使用发布前备份和完整一致性点，而不是删除投影表。

Projection policy 的版本升级不是普通进程滚动升级，必须遵循 `docs/LEARNER_PROJECTION_FAILURE_RECOVERY.md` 的版本化 rebuild 流程。禁止用同一个版本字符串发布不同算法。

## 7. 明确范围

本阶段只部署 Agent Runtime、Agent Turn 必需事务与 Learner Projection 闭环，不包含：

- Skill Patch 生成、应用或 PatchDecision；`patch_eligible` 始终为 false；
- 飞书助手、审批、通知或消息投递；
- Product REST；Learner rebuild 不是新增外部 HTTP API；
- World WSS；
- Godot 或其他前端 UI；
- LangGraph、MCP、Central Lane 或新的通用 Memory 系统。
