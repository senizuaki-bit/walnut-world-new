# 契约规则

## Game HTTP 当前尝试与资源来源上下文

Game Wire API 将一次 HTTP 调用固定表示为 `WireAttemptContext`，只包含
`schema_version + request_id + trace_id + correlation_id`，分别映射到必填请求头
`X-Schema-Version + X-Request-Id + X-Trace-Id + X-Correlation-Id`。所有成功和失败响应都必须在
`X-Request-Id + X-Trace-Id + X-Correlation-Id` 中回显当前 HTTP 尝试，不得静默生成、遗漏或复用旧尝试身份。

资源响应体中的 `request_context` 是产生该资源的不可变 domain/origin context。后续 GET 轮询可以且应该使用新的
request、trace 和 correlation；服务端不得用轮询身份覆盖资源来源上下文，Godot Gateway 也不得把两者做全字段相等比较。
Gateway 仍须严格验证响应体 Schema、path 中的资源 ID、认证 actor 与来源 actor，跨 actor/tenant 读取一律失败。

Bootstrap 发生在 actor/content_ref 被服务端解析之前，因此 `get_bootstrap` 只接收 `WireAttemptContext`，调用端不得伪造
完整 `RequestContext`。认证后的 actor、内容版本和 domain context 由 Bootstrap 响应提供。

首次 Accepted `202` 必须满足 payload `trace_id == X-Trace-Id`。幂等回放继续返回首次接受时的 payload/原始 trace，
但三个响应身份头始终回显当前重试尝试；不得为通过校验而要求重试复用旧 request/trace/correlation。

## 单一事实源

`contracts/` 是前端、后端、Sandbox、飞书适配器之间的唯一接口事实源。实现代码不得复制一份含义不同的 DTO；客户端类型、验证器和 Mock 数据都应从这里派生。

## 写请求

所有创建、修改或触发执行的 HTTP 请求必须携带：

- `Authorization`
- `X-Request-Id`
- `X-Trace-Id`
- `X-Correlation-Id`
- `Idempotency-Key`
- `X-Schema-Version`

精确的 `content_ref` 必须存在于请求体、已固定版本的 Agent Session 或被引用的不可变资源中，不能依赖服务端当前的 `latest` 内容。

同一个幂等键与相同请求体返回第一次请求的结果；同一个幂等键与不同请求体必须返回 `409 IDEMPOTENCY_KEY_REUSED`。

幂等作用域至少包含 `tenant_id + actor_id + operation + idempotency_key`。只要回执或其资源按 actor 隔离，幂等记录也必须使用同一 actor 边界；禁止向同租户的另一个 actor 回放不可对账的 `command_id`。业务状态变更与原始幂等回执必须原子持久化：即使序列化、网络或客户端随后失败，使用同一键重试也必须取回原命令，不能再次执行或因状态已推进而返回冲突。

命令存储的首次接收接口返回 `CommandCreateReceipt(command, created)`。只有 `created=true` 的调用方可以投递执行；`created=false` 表示命中已有命令，只能返回或对账，禁止再次产生副作用。

命令状态变更使用 `CommandTransition(previous_record, next_record)`。Adapter 以 previous record 派生的 `command_id + revision + status` 执行单条 CAS，并在写入前拒绝对 `request_context / command_type / accepted_at / versions / self link` 等不可变身份的修改。修订号只能 `+1`，终态不得再转移，状态边必须属于 Python/TypeScript 公布的封闭状态图。

所有命令、Build、Session、Activation、Evidence、飞书 Release/Report 和事件去重键都必须按租户分区。知道另一个租户的 ID 不能读取资源；建议用 `404` 隐藏其存在。

## 传输结果

Adapter 必须保留 HTTP 状态和响应头。Godot Transport 只允许以下两种精确形状：

```text
{ ok: true,  status, headers, value }
{ ok: false, status, headers, error }
```

`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`Location`、`Retry-After`、`ETag` 和 `X-World-Revision` 均属于可测试契约，不得在 Adapter 内丢失。服务端错误码对应的 HTTP 状态必须与错误目录一致。

所有 `format: date-time` 字段严格使用带时区的 RFC 3339。Adapter 不得接受 basic ISO 格式、空格分隔、非法公历日期或非法 UTC offset。

Accepted `202` 响应必须携带 `Idempotency-Replayed: false|true`。`false` 表示首次持久化接收，此时 payload `trace_id` 必须等于本次请求 trace；`true` 表示返回原始幂等回执，payload 保留首次创建命令的 trace。无论是否回放，`X-Request-Id`、`X-Trace-Id` 和 `X-Correlation-Id` 响应头始终绑定当前 HTTP 尝试，客户端不得要求重试复用旧 request/trace/correlation 来掩盖身份混淆。

## 源码包与传输限制

Bootstrap 固定声明 `max_source_files=32`、`max_source_bytes=1048576`。Skill Build 请求必须同时满足：文件数不超过 32，且所有 `files[*].content` 的 UTF-8 字节数总和不超过 1 MiB。字符数、JSON 字符串长度或单文件长度都不能替代 UTF-8 总字节统计。

HTTP 原始请求体采用独立的 8 MiB 传输上限，用于容纳 1 MiB 源码、JSON 转义膨胀和必要元数据。超过源码业务上限返回 `400 INVALID_REQUEST`；超过 HTTP 原始请求体上限返回 `413 PAYLOAD_TOO_LARGE`，两者不得混用。

## 异步操作

编译、测试、Agent Turn、报告生成和内容发布均为异步操作。提交成功返回 `202` 和稳定的 `command_id` 或 `job_id`。`202` 只表示已接收，不代表业务完成。

每个 `ACCEPTED` 命令必须持久化并最终进入：

```text
APPLIED | REJECTED | FAILED | CANCELLED
```

调用方超时且无法判断是否已经提交时，状态为 `UNKNOWN`。调用方必须使用原 `command_id` 对账，禁止换新幂等键盲目重试。

## 世界成功语义

Sandbox 成功只表示代码产生了合法 `ActionIntent[]`。只有 WorldEngine 返回 `WorldCommitReceipt` 后，前端才能显示“世界动作成功”。

WorldEngine 必须通过唯一写边界 `WorldUnitOfWorkPort` 在同一事务中提交世界状态、Domain Event 和 Outbox。`WorldPort` 只读；任何模块不得绕过 UoW 修改世界数据库。UoW 请求必须显式携带 `stream_id` 和 `expected_stream_sequence`，不得依赖隐式 world→stream 约定。

成功的世界提交必须满足 `world_revision = previous_revision + 1`；命令结果、运行回执和 `world.committed` 事件三处都执行同一条不变量。

`WorldAtomicCommitReceipt.stream_id` 必须同时等于请求的 `WorldAtomicCommit.stream_id` 和事件回执的 `EventAppendReceipt.stream_id`。不允许从 `world_id` 隐式推导 stream，也不允许将其他域的有效事件回执组装成世界原子提交回执。

Session 与 Turn 必须指向当前租户真实存在的 World。Turn 提交前还要核对世界修订、客户端事件游标、客户端 turn 序号，以及 `skill_id + skill_version_id + certification_id + artifact_sha256` 的完整认证绑定；任一失败都不得推进世界。

## 事件

事件采用至少一次投递。消费者必须按 `event_id` 幂等，并按同一 `stream_id` 的 `sequence` 检测缺口。发现缺口后停止应用增量，补拉权威快照。

每条事件都必须包含 `trace_id`、`command_id`、`correlation_id`、`causation_id` 和精确的 `content_ref`。

### 客户端 WebSocket 与恢复

冻结 Bootstrap Schema 的 `world` 必须同时返回 `stream_id + stream_url + last_event_sequence + stream_protocol_version`，即使 `world_event_stream` capability=false 也不能省略字段。Capability 是可用性权威：false 时 `stream_url` 只是 inert 结构值，客户端不得连接；只有 true 时才进入 Upgrade 规则。`stream_url` 只允许无 query、无 fragment、无内嵌凭据的 `wss://` URL；生产环境使用 JWT Bearer，`<tenant_id>:<actor_id>` 仅是本地 Mock 测试凭据，不是生产 token 格式。Upgrade 必须携带 AsyncAPI `WebSocketUpgradeHeaders` 中列出的全部应用头，并协商子协议 `yaya.runtime.v1`。

客户端只有在已经原子应用 Bootstrap 世界视图或本地持久化投影后，才能用其最高连续游标发送 `subscribe.after_sequence`。服务端回复 `subscribed` 后，从 `accepted_after_sequence + 1` 开始回放，再切换到实时推送。重连使用 `resume`；服务端采用至少一次投递，因此可以重复发送已观察但尚未确认的事件。

客户端确认规则固定为：

- 先原子提交本地投影与 checkpoint，再发送 `ack`；禁止先确认后应用。
- `ack.sequence` 是该 `stream_id` 已持久化应用的最高连续序号，`ack.event_id` 必须是该序号对应的事件。
- 按 `event_id` 去重；相同 `stream_id + sequence` 若出现不同 `event_id` 或非字节等价事件，按 `INVARIANT_VIOLATION` 处理，禁止择一静默继续。
- 心跳必须用同一 `subscription_id + nonce` 回复 `heartbeat_ack`；超过服务端声明的存活窗口可用 `4408` 关闭，客户端从最后持久化 checkpoint 恢复。

发现序号缺口后，客户端必须暂停应用后续实时增量，使用 Bootstrap 的 `events_url` 并以最后持久化序号作为 `after_sequence` 拉取 HTTP backfill。WSS `WorldEvent` 直接引用 `world-event-page.events.items`，两条通路必须通过同一验证器。只有补齐连续缺口后才可继续；如果 backfill 返回 `EVENT_SEQUENCE_GAP`、保留窗口已过或数据互相矛盾，则获取 `snapshot_url`，原子替换本地世界，将 checkpoint 更新为 `snapshot.last_event_sequence`，随后发送 `resume`。

AsyncAPI `RealtimeErrorFrame.x-close-codes` 是应用关闭码和错误目录的唯一映射。`fatal=true` 时必须发送非空 `close_code` 后关闭；`fatal=false` 时 `close_code` 必须为空。未知帧、未知字段、错误协议版本和错误流身份都必须显式返回错误或关闭，不得忽略。

## 错误

错误只能使用 `contracts/error-catalog.json` 中的稳定代码。不得使用空对象、空字符串、布尔值或自然语言表示失败；不得返回 `200 + success:false`。

未知异常转换成 `INTERNAL_ERROR`，保留内部堆栈并产生告警，但不得向学生端泄露堆栈、源码、密钥或隐私数据。

## AI 降级

确定性提示可作为有效降级结果返回 `200`，但响应必须明确包含：

```json
{
  "degraded": true,
  "source": "provider_fallback",
  "fallback_reason": "MODEL_OUTPUT_INVALID"
}
```

前端必须识别并记录降级；不得伪装成模型成功。

## 飞书边界

`tenant_id + approval_instance_id` 是不可变审批实例键。首次成功决策原子保存规范化业务决策与原始 receipt；完全相同的业务决策用其他幂等键重放时返回原 receipt，不再次递增 `candidate_revision`。复用同一审批实例但改变候选、决策或 actor 时返回 `409 CONTENT_VERSION_MISMATCH`，且不改变候选状态。`REJECT` 产生 `WORKFLOW_CLOSED` 后，同一候选的后续新审批实例也必须返回冲突并保持修订不变。

飞书仅用于内容候选、审核、查询、报告和通知，不进入游戏实时世界提交链。飞书草稿或 Webhook 不能直接激活线上 Policy、Skill 或世界状态。

所有发往飞书的写操作通过持久化 Outbox，状态为：

```text
PENDING -> SENDING -> SENT
                  -> RETRYING -> DEAD_LETTER
```

飞书不可用不得阻塞游戏运行。

Outbox 状态必须是闭合互斥联合：

- `PENDING`：`attempt=0`，不得带 lease、retry、error 或 receipt。
- `SENDING`：必须带当前 attempt 和未过期 lease，不得带终态/retry 字段。
- `SENT`：必须带与 message/attempt 一致的 `DeliveryReceipt`，不得残留 lease、error 或 retry 时间。
- `RETRYING`：必须带 `last_error + next_attempt_at`，不得残留 lease/receipt。
- `DEAD_LETTER`：必须带 `last_error + dead_lettered_at`，不得再带 retry/lease/receipt。

Outbox 入队幂等作用域固定为 `(tenant_id, destination, idempotency_key)`：同作用域且同 `payload_sha256` 返回原记录，不同 hash 必须显式返回 `IDEMPOTENCY_KEY_REUSED`。这是服务出站投递的明确例外，不是 actor 所有的命令回执；原始 actor 必须保留在 `operation_context` 供审计，但不得加入 Outbox 去重键，否则同一租户级投递可能被不同 worker/service actor 重复发送。

## 访问审计

所有标记 `x-audit-access: true` 的读操作在返回业务结果前，必须通过 `AuditPort` 追加符合 `contracts/schemas/common/audit-record.schema.json` 的记录。记录只保存 hash/脱敏资源引用与必要元数据，`redacted` 固定为 `true`，不得放入学生原始对话、源码、密钥或直接身份标识。

## 已持久化但接受回执出站失败

如果命令及幂等回执已持久化，但 `202` 在出站序列化阶段失败，服务端不得返回不可对账的普通 `INTERNAL_ERROR`。必须返回 `503 UNKNOWN_COMMIT_STATE`、`retryable=false`、原 `command_id` 与 `Location: /v1/commands/{command_id}`；客户端只轮询该 Location，不创建新命令。使用原请求体和原幂等键重放仍必须返回同一命令，作为网络级恢复手段。

## 兼容性

- 当前 `schema_version=1.0.0` 采用闭合对象：请求与响应都拒绝未知字段，避免拼写错误或版本漂移被静默吞掉。
- 在同一 schema 版本中增加字段（即使拟定为可选）、删除字段、改变字段类型、收窄枚举或改变语义，都属于破坏性变更。
- 字段演进必须发布新的 schema/API 版本并提供迁移期；新旧消费者按明确版本选择验证器，不得靠忽略未知响应字段“兼容”。
