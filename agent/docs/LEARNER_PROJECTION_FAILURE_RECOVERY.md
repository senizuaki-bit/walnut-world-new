# Learner Projection 故障恢复

> INT1 ownership（2026-08-12）：以下恢复原则继续适用，但 production owner 是 sibling `walnut-world-backend` terminal projection 与 Backend-owned PostgreSQL；Agent 私有 learner tables/worker 只保留历史回归用途。

## 1. 恢复原则

Learner Projection 是可重建的派生状态，但 source inference、AgentTurn、Run、World、Evidence、Event hash 和 command authority 都是不可变事实。故障处理必须遵循：

- 不回滚或重写已经提交的 Run、World、Evidence、AgentDecision 或 source Event；
- 不跳过 source sequence，不给事件重新编号；
- 不改 JSON 后补写 hash，不复制 receipt/Outbox 获得“成功”；
- 不使用新 event ID 或 command ID 掩盖同一 source event 的不确定状态；
- 对外继续提供最后一个成功 Learner revision；
- 只有 source receipt 能证明投影成功，只有 fenced failure record 能证明投影终结；
- 恢复仍通过 LearnerPort 和后端事务边界完成，禁止直接更新 snapshot JSON。

Job 状态是 `READY | LEASED | SUCCEEDED | FAILED`。Failure 分类是：

| 分类 | 含义 | 自动动作 |
|---|---|---|
| `RETRYABLE` | 数据库/依赖暂不可用、sequence/CAS 需重读、提交待对账 | fenced 释放为 READY，保留 attempt 历史，延后重试 |
| `PERMANENT` | 已验证的 schema、identity、hash、authority 或 policy 输入错误 | 原子写 failure、`learner.projection.failed`、Outbox 和 FAILED |
| `QUARANTINED` | Worker 自身异常或无法安全归类的 durable invariant 故障 | FAILED 并阻塞该 learner 后续 sequence，等待人工审查 |

未解决的 PERMANENT/QUARANTINED failure 会阻止同一 learner 后续 job 被领取，避免在缺口之后继续推进。

## 2. 快速分诊

先记录 tenant、learner、job、event、source sequence、attempt/fence 和当前 snapshot checkpoint。所有查询应只读：

```sql
SELECT tenant_id, job_id, event_id, source_event_id, learner_id,
       source_stream_id, source_stream_sequence, state, attempt,
       fencing_token, worker_id, lease_id, lease_expires_at,
       last_error_code, available_at, updated_at
FROM yaya_learner_projection_jobs
WHERE tenant_id = $1 AND learner_id = $2
ORDER BY source_stream_sequence;

SELECT revision, projected_through_sequence, projection_policy_version,
       updated_at
FROM yaya_learner_models
WHERE tenant_id = $1 AND learner_id = $2;

SELECT failure_id, job_id, source_stream_sequence, attempt,
       classification, error_code, error_sha256, recorded_at,
       resolved_at, resolution
FROM yaya_learner_projection_failures
WHERE tenant_id = $1 AND learner_id = $2
ORDER BY recorded_at;
```

不要把真实 tenant/learner 值拼接进 SQL；运维工具必须使用绑定参数。先保留数据库快照和相关日志，再选择下列恢复路径。

## 3. Sequence gap 或乱序

Worker 只领取 `source_stream_sequence = projected_through_sequence + 1` 的 job。后续 sequence 已到而前一条缺失时，它们保持 READY；这不是可以跳过的 backlog。

用窗口查询定位第一处缺口或重复：

```sql
WITH ordered AS (
  SELECT tenant_id, learner_id, source_stream_sequence,
         lag(source_stream_sequence) OVER (
           PARTITION BY tenant_id, learner_id
           ORDER BY source_stream_sequence
         ) AS previous_sequence
  FROM yaya_learner_projection_jobs
  WHERE tenant_id = $1 AND learner_id = $2
)
SELECT *
FROM ordered
WHERE (previous_sequence IS NULL AND source_stream_sequence <> 1)
   OR (previous_sequence IS NOT NULL
       AND source_stream_sequence <> previous_sequence + 1);
```

按以下顺序处理：

1. 检查缺失 sequence 是否存在于 `yaya_events` 的同一 `learner:<learner_id>` 源流。
2. 检查 source Event、job、job evidence 是否由同一次 AgentTurn commit 全部可见；正常事务不允许只出现其中一部分。
3. 检查前一条 job 是否 FAILED，或其 source receipt 已存在但 snapshot/job 状态没有收敛。
4. 若只是并发 CAS，保持原 event/job 不变，让 Worker 重读 revision；不要创建替代 job。
5. 若发现部分事务、异 hash、跨 authority 或不可恢复的源缺失，停止该 learner 的自动恢复，保留证据并按 durable invariant incident 处理。只能从一致备份恢复原字节，不能人工合成事件。
6. 源流恢复连续后，从最后一个成功 checkpoint 执行受控 rebuild，再恢复 Worker。

如果 snapshot checkpoint 已经越过某 sequence，但该 source receipt 不存在，属于 `MISSING_SOURCE_RECEIPT` 级别的不变量故障。不要倒改 checkpoint；隔离后从不可变源执行 rebuild 并核对全部 receipt/派生记录。

## 4. `learner.projection.failed`

确定性投影失败不会回滚原 Agent Turn、Run、World 或 Evidence。fenced failure 事务必须同时写：

- `yaya_learner_projection_failures` 的 PERMANENT 记录；
- 派生流上的 `learner.projection.failed`；
- 对应 Outbox；
- job `FAILED` 与同一 `last_error_code`。

此时读取路径继续返回最后一个成功 snapshot/revision。分诊时核对 error code、redacted error JSON/hash、失败事件和 Outbox 是否完整；任何部分可见都表示原子性故障，不可通过补一行 SQL 修复。

常见原因及动作：

| 原因 | 动作 |
|---|---|
| schema/event version 不支持 | 部署明确支持该版本的代码；旧算法必须继续可寻址 |
| tenant/actor/content/session/turn/run/command 错链 | 保留失败，不重试；修复上游生产者后仅对新的合法 turn 生效 |
| source/Event/Evidence/turn hash 漂移 | 从权威备份恢复原字节或宣布数据损坏；禁止重算 hash 迁就现值 |
| projection policy bug | 发布新的 policy version，完成回放对比后走版本升级 rebuild |
| 暂时数据库故障被错误归为永久 | 修复分类代码并审计；使用受控 recovery transaction 重新排队，不能复制 job |

修复后，failure 的 `resolved_at + resolution` 与 job 重试/重建状态必须由一个受审计的后端恢复事务写入。`resolution` 只能是 `RETRIED | REBUILT | DISMISSED`。当前阶段不暴露 Product REST 恢复接口；运维通过内部维护入口调用 LearnerPort，不直接编辑表。

## 5. `UNKNOWN_COMMIT_STATE`

`UNKNOWN_COMMIT_STATE` 表示投影事务主体已完成，但客户端没有收到 PostgreSQL 对 COMMIT 的确定确认。它既不是成功，也不是已知 rollback。Worker 必须先按原 `(tenant_id, event_id, job_id)` 对账，禁止立即再次应用 policy。

检查完整成功组：

```sql
SELECT j.state, j.event_sha256, j.inference_sha256,
       r.event_sha256 AS receipt_event_sha256,
       r.inference_sha256 AS receipt_inference_sha256,
       r.previous_learner_revision, r.learner_revision,
       r.model_updated_event_id, r.outbox_message_id
FROM yaya_learner_projection_jobs AS j
LEFT JOIN yaya_learner_projection_receipts AS r
  ON r.tenant_id = j.tenant_id
 AND r.job_id = j.job_id
 AND r.event_id = j.event_id
WHERE j.tenant_id = $1 AND j.job_id = $2;
```

恢复判定：

- receipt 可见且 event/inference hash、revision、derived event 和 Outbox 全部匹配：事务已提交，返回原 `LearnerUpdate`，将其视为幂等成功，不再推进 revision；
- receipt 不可见且 PostgreSQL 明确恢复：原 lease 仍有效时可 fenced retry；lease 已过期则由新 Worker takeover；
- PostgreSQL 仍不可查询：保持不确定，告警并等待，不能写 fallback snapshot；
- receipt 可见但 snapshot、job、derived event 或 Outbox 不一致：durable invariant incident，立即隔离；
- job 显示 SUCCEEDED 但 receipt 不存在：不得重跑或伪造 receipt，按数据损坏处理。

唯一 receipt、Event、Outbox 和 learner revision CAS 共同保证重放不重复推进。不要改用新的 event ID、idempotency key 或 source sequence。

每次 Job 进入 `SUCCEEDED`、永久 `FAILED` 或隔离 `FAILED` 时，数据库触发器会在同一终态事务中追加一条不可丢失的 `yaya_learner_projection_terminal_audits` 义务。Worker 每轮领取新 Job 前优先处理未验证义务：成功图必须闭合 snapshot/receipt/derived Event/Outbox；永久失败图必须闭合 failure/derived Event/Outbox；隔离图只允许闭合 Job 与精确的 QUARANTINED failure，不能伪造 receipt 或派生成功。只有只读对账结果为 `MATCH` 才能把 audit 的 `verified_at` 从 NULL 更新为数据库时钟；`MISSING` 或 `CORRUPT` 必须使 Worker 失败并保留终态原字节供事故调查。即使 COMMIT 已成功而紧随其后的查询连接中断，重启后的 Worker 也会继续这条义务，不能把终态 `FenceLost` 当作已经对账。

## 6. Worker 崩溃、heartbeat 与 takeover

所有时间判断使用 PostgreSQL `clock_timestamp()`。Worker 每个 claim 生成随机 `lease_id`，并把 `attempt` 与单调增长的 `fencing_token` 同步增加。heartbeat 与所有最终写入都再次校验：

```text
tenant_id + job_id + state=LEASED
+ worker_id + lease_id + fencing_token
+ lease_expires_at > database clock
```

进程崩溃时无需手工释放。等待 lease 到期，新 Worker 使用更大的 token 领取。旧 Worker 的 heartbeat、snapshot、receipt、failure 和状态更新都必须影响零行。若数据库中断导致 heartbeat 丢失，同样按 takeover 处理；不要相信进程本地时钟。

只有 `DEPENDENCY_UNAVAILABLE`、`EVENT_SEQUENCE_GAP`、`RATE_LIMITED` 和 `UNKNOWN_COMMIT_STATE` 等明确可恢复故障进入 retry。未知异常应 quarantine，不能形成无限 poison-job 循环。

## 7. 确定性 rebuild

`LearnerPort.rebuild(learner_id, through_sequence, context)` 是唯一重建边界。它必须从 sequence 1 到指定连续 checkpoint 读取不可变 `learner.inference.recorded` 与对应 Evidence/AgentTurn 事实，不能读取当前 stage、mastery、review time 或派生 `learner.model.updated` 作为事实输入。

操作步骤：

1. 停止产生该 learner 的新教学 turn；当前阶段没有 per-learner 外部控制 API时，应优雅停止全部 Learner Worker。
2. 等待所有未过期 LEASED job 收敛或自然过期，并创建数据库一致性备份。
3. 选择最高连续且已验证的 `through_sequence`；逐条重算 Event、inference、turn commit 和 Evidence hash，并核对 tenant/actor/content/session/turn/run/command。
4. 使用精确 actor、content_ref、command/request context 通过受审计的内部维护入口调用 `LearnerPort.rebuild`。禁止直接 `UPDATE yaya_learner_models`。
5. rebuild 以 revision CAS 原子替换 snapshot；相同 source、相同 policy version、相同 through sequence 的结果必须与在线投影字节等价。
6. rebuild 不重新发模型请求、不运行 Sandbox、不追加 source inference，不重复写 source receipt、`learner.model.updated` 或 Outbox。
7. 核对 snapshot hash、revision、checkpoint、competencies、Evidence 集合和 policy version；将相关 failure 以 `REBUILT` 方式受审计地解决。
8. 先启动一个 Learner Worker，确认只从 `through_sequence + 1` 继续，再恢复全部实例和 Agent Turn 流量。

rebuild 不允许越过 `READY`/`LEASED` job，也不允许低于由 receipt 与已 `REBUILT` failure 计算出的不可变 applied lower bound。当前 snapshot 只有在 provenance、`snapshot_sha256`、row/snapshot identity、`revision = projected_through_sequence` 和 applied marker 全部闭合时才可作为防回退依据；合法 head 不得回退。若派生 row 的 checkpoint 被损坏性抬高或 provenance/hash 不可信，可在不低于 applied lower bound 的真实 source head 进行受控重建替换，不能把不可信 checkpoint 当作事实。成功 rebuild 会在同一事务中把范围内尚未解决的 failure 标为 `REBUILT`；成功重试会把该 job 的旧 `RETRYABLE` failure 标为 `RETRIED`。若 rebuild 发现 active job、sequence gap、Evidence hash 漂移或 authority 错链，必须失败且保持旧成功 snapshot 可读。不要缩短可信 checkpoint 来隐藏损坏，也不要跳过坏事件。

`learner_projection_v1` 对 snapshot Evidence 使用确定性的 source-order tail-64 压缩，并同步过滤 competency 证据引用；完整审计历史不从 source Event、Job Evidence 或 receipt 中删除。rebuild 必须复现与在线投影相同的有界 snapshot。

## 8. Projection policy 版本升级

Policy version 是持久化语义的一部分，不是部署标签。版本升级遵循：

1. 已发布版本的代码和 review interval 不可原地修改；新增例如 `learner_projection_v2` 的实现和测试向量。
2. 新二进制必须同时能读取现存 snapshot 的旧版本，并能按明确版本选择 policy；禁止把未知版本默认为 latest。
3. 暂停目标 cohort 的新 inference 和 Learner Worker，在影子环境分别执行旧版本 rebuild 与新版本候选 rebuild。
4. 旧版本 rebuild 必须与线上同 checkpoint snapshot 一致；不一致先按数据/实现故障处理，不能继续升级。
5. 经审批后，用明确目标版本执行受控 rebuild，保存升级前后 snapshot/hash/checkpoint、代码摘要和操作者审计记录。
6. 更新 `projection_policy_version` 与 snapshot 必须同一 CAS 事务完成；恢复 Worker 后，新 job 只能使用该明确版本。
7. 回滚也必须是一次显式版本化 rebuild，不能只改版本字符串。

如果要改变历史 inference 的含义，这属于新的投影代际迁移，不是普通 retry；必须有独立 migration、版本和审计方案。routine rebuild 始终复现被指定 policy version 的在线结果。

## 9. 事件与 Outbox 对账

成功投影的 snapshot、source receipt、`learner.model.updated`、Outbox 和 job SUCCEEDED 必须全有或全无。永久失败的 failure record、`learner.projection.failed`、Outbox 和 job FAILED 也必须全有或全无。

Outbox 仍以稳定 idempotency key 和 payload SHA-256 去重。下游响应丢失只重放原消息；不得复制 payload 为新的 message ID。Outbox DEAD_LETTER 不会回滚已经成功的 Learner snapshot，修复投递后继续用原消息对账。

## 10. 明确范围

本恢复流程不授权或实现 Skill Patch、飞书、Product REST、World WSS、Godot/前端 UI、LangGraph、MCP、Central Lane 或新的通用 Memory。尤其不能为了恢复 Learner Projection 而调用模型重新生成 inference、重新运行 C++、修改 World，或新增一个未冻结的管理 API。
