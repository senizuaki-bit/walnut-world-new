# HTTP 接口字段参考

更新日期：2026-09-09。由实际挂载路由与本地合同生成；包含默认关闭但可配置启用的路由。先读 [联调主文档](README.md)。

请求头和异步流程见主文档。普通接口直接返回资源 JSON，不包 `{code,data}`；MCP 是 JSON-RPC。示例是合同样例，ID、版本、哈希需替换为当前接口返回值，不是现成可用的业务数据。

通用 `request_context` 与 `versions` 的完整字段见文末公共结构；字段表最多展开三层，深层结构及互斥条件以链接的 Schema 为准。

| 操作 | 方法与路径 | 可用条件 |
| --- | --- | --- |
| [兼容启动信息](#getgamebootstrap) | `GET /v1/bootstrap` | 路由已挂载；仍需有效身份与资源 |
| [查询异步处理状态](#getcommand) | `GET /v1/commands/{command_id}` | 路由已挂载；仍需有效身份与资源 |
| [读取运行结果](#getrun) | `GET /v1/runs/{run_id}` | 路由已挂载；仍需有效身份与资源 |
| [读取原始证据](#getevidence) | `GET /v1/evidence/{evidence_id}` | 路由已挂载；仍需有效身份与资源 |
| [读取世界快照](#getworldsnapshot) | `GET /v1/worlds/{world_id}/snapshot` | 路由已挂载；仍需有效身份与资源 |
| [读取世界事件](#listworldevents) | `GET /v1/worlds/{world_id}/events` | 路由已挂载；仍需有效身份与资源 |
| [读取世界动画事件](#listworldpresentationevents) | `GET /v1/worlds/{world_id}/presentation-events` | WALNUT_ENABLE_WORLD_PRESENTATION=true |
| [学生启动信息](#getstudentbootstrap) | `GET /v1/student-bootstrap` | 路由已挂载；仍需有效身份与资源 |
| [教师查询学生学习记录](#querylearnerprojectionfromfeishu) | `POST /integrations/feishu/v1/learner-queries` | 路由已挂载；仍需有效身份与资源 |
| [教师查询班级统计](#queryclassinsightsfromfeishu) | `POST /integrations/feishu/v1/class-insights` | 路由已挂载；仍需有效身份与资源 |
| [教师读取脱敏证据](#getredactedevidenceforfeishu) | `GET /integrations/feishu/v1/evidence/{evidence_id}` | 路由已挂载；仍需有效身份与资源 |
| [教师只读 MCP 工具](#feishuteachermcp) | `POST /integrations/feishu/v1/mcp` | 路由已挂载；仍需有效身份与资源 |
| [提交编译](#createskillbuild) | `POST /v1/skill-builds` | 路由已挂载；仍需有效身份与资源 |
| [读取编译结果](#getskillbuild) | `GET /v1/skill-builds/{build_id}` | 路由已挂载；仍需有效身份与资源 |
| [激活技能版本](#activateskillversion) | `POST /v1/skill-versions/{skill_version_id}/activations` | 路由已挂载；仍需有效身份与资源 |
| [读取激活结果](#getskillactivation) | `GET /v1/skill-activations/{activation_id}` | 路由已挂载；仍需有效身份与资源 |
| [创建会话](#createagentsession) | `POST /v1/agent-sessions` | 路由已挂载；仍需有效身份与资源 |
| [读取会话与 Turn 序号](#getagentsession) | `GET /v1/agent-sessions/{session_id}` | 路由已挂载；仍需有效身份与资源 |
| [运行技能／文字提问／请求代码建议](#createagentturn) | `POST /v1/agent-sessions/{session_id}/turns` | 路由已挂载；仍需有效身份与资源 |
| [批量上报客户端事件](#ingestclienteventbatch) | `POST /v1/client-events:batch` | WALNUT_ENABLE_CLIENT_EVENT_BATCH=true |
| [读取代码草稿](#getproductskilldraft) | `GET /product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}` | 路由已挂载；仍需有效身份与资源 |
| [保存代码草稿](#upsertproductskilldraft) | `PUT /product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}` | 路由已挂载；仍需有效身份与资源 |
| [读取关卡内容](#getproductcontentunit) | `GET /product-experience/v1/content-units/{unit_id}/versions/{content_version}` | 路由已挂载；仍需有效身份与资源 |
| [查询可用功能](#getint2capabilities) | `GET /product-experience/v1/capabilities` | 路由已挂载；仍需有效身份与资源 |
| [读取反馈／提问／总结列表](#listproductagentinteractions) | `GET /product-experience/v1/sessions/{session_id}/agent-interactions` | 路由已挂载；仍需有效身份与资源 |
| [读取单条反馈及建议决定](#getproductagentinteraction) | `GET /product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}` | 路由已挂载；仍需有效身份与资源 |
| [接受或拒绝代码建议](#recordproductpatchdecision) | `POST /product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}/patches/{patch_id}/decision` | WALNUT_ENABLE_SKILL_PATCH=true（同时要求 WORLD_PRESENTATION） |
| [恢复工作区](#getproductsessionworkspace) | `GET /product-experience/v1/sessions/{session_id}/workspace` | 路由已挂载；仍需有效身份与资源 |

## getGameBootstrap

**兼容启动信息** · `GET /v1/bootstrap`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |

请求体：无。

### 响应

**HTTP 200**：Bootstrap data with authoritative world cursor

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/bootstrap-response.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `api_version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+$` |  |
| `server_time` | 是 | `string; format=date-time` |  |
| `actor` | 是 | `object; 不接受额外字段` |  |
| `actor.tenant_id` | 是 | `string; pattern=^[A-Za-z0-9_-]{3,96}$` |  |
| `actor.actor_id` | 是 | `string; pattern=^[A-Za-z0-9_-]{3,128}$` |  |
| `actor.actor_type` | 是 | `枚举 student, agent, teacher, researcher, operator, service` |  |
| `actor.roles` | 是 | `array; maxItems=16` |  |
| `content` | 是 | `object; 不接受额外字段` |  |
| `content.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `capabilities` | 是 | `object; 不接受额外字段` |  |
| `capabilities.skill_builds` | 是 | `boolean` |  |
| `capabilities.agent_sessions` | 是 | `boolean` |  |
| `capabilities.world_event_stream` | 是 | `boolean` |  |
| `capabilities.client_event_batch` | 是 | `boolean` |  |
| `capabilities.evidence_query` | 是 | `boolean` |  |
| `limits` | 是 | `object; 不接受额外字段` |  |
| `limits.max_source_files` | 是 | `固定 32` | Maximum number of files in one SkillSourceBundle. |
| `limits.max_source_bytes` | 是 | `固定 1048576` | Maximum sum of UTF-8 content bytes across one SkillSourceBundle. |
| `limits.max_client_events_per_batch` | 是 | `integer; minimum=1` |  |
| `limits.max_agent_turn_chars` | 是 | `integer; minimum=1` |  |
| `world` | 是 | `object; 不接受额外字段` |  |
| `world.world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `world.revision` | 是 | `integer; minimum=0` |  |
| `world.stream_id` | 是 | `string; pattern=^[A-Za-z][A-Za-z0-9:_-]{2,159}$` |  |
| `world.last_event_sequence` | 是 | `integer; minimum=0` |  |
| `world.stream_protocol_version` | 是 | `固定 "1.0.0"` |  |
| `world.snapshot_url` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `world.events_url` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `world.stream_url` | 是 | `string; format=uri; maxLength=2048; pattern=^wss://[^@/?#]+(?:/[^?#]*)?$` |  |

完整 JSON 示例：[examples/game-bootstrap-response.json](examples/game-bootstrap-response.json)。

合同声明的错误状态：400、401、403、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getCommand

**查询异步处理状态** · `GET /v1/commands/{command_id}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `command_id` | path | 是 | string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$ |

请求体：无。

### 响应

**HTTP 200**：Current command state; only APPLIED with WORLD_COMMIT proves a world mutation

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/command.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `revision` | 是 | `integer; minimum=1` |  |
| `command_type` | 是 | `枚举 CREATE_SKILL_BUILD, ACTIVATE_SKILL_VERSION, CREATE_AGENT_SESSION, EXECUTE_AGENT_TURN, INGEST_CLIENT_EVENTS` |  |
| `status` | 是 | `枚举 ACCEPTED, VALIDATING, RUNNING_SANDBOX, APPLYING_WORLD, APPLIED, REJECTED, FAILED, UNKNOWN, CANCELLED` |  |
| `stage` | 是 | `枚举 ACCEPT, VALIDATE, POLICY, REGISTRY, SANDBOX, WORLD_VALIDATE, WORLD_COMMIT, EVIDENCE, COMPLETE` |  |
| `terminal` | 是 | `boolean` |  |
| `accepted_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `result` | 是 | `object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段 / null` |  |
| `result[分支1].result_type` | 是 | `固定 "WORLD_COMMIT"` |  |
| `result[分支1].world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `result[分支1].previous_revision` | 是 | `integer; minimum=0` |  |
| `result[分支1].world_revision` | 是 | `integer; minimum=1` |  |
| `result[分支1].first_event_sequence` | 是 | `integer; minimum=1` |  |
| `result[分支1].last_event_sequence` | 是 | `integer; minimum=1` |  |
| `result[分支2].result_type` | 是 | `固定 "RESOURCE_CREATED"` |  |
| `result[分支2].resource_type` | 是 | `枚举 SKILL_BUILD, AGENT_SESSION, SKILL_ACTIVATION` |  |
| `result[分支2].resource_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `result[分支2].resource_url` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `result[分支3].result_type` | 是 | `固定 "CLIENT_EVENTS_ACCEPTED"` |  |
| `result[分支3].batch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `result[分支3].accepted_count` | 是 | `integer; minimum=0` |  |
| `result[分支3].duplicate_count` | 是 | `integer; minimum=0` |  |
| `result[分支3].rejected_count` | 是 | `integer; minimum=0` |  |
| `result[分支4].result_type` | 是 | `固定 "NO_EFFECT"` |  |
| `result[分支4].reason_code` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,95}$` |  |
| `error` | 是 | `object; 不接受额外字段 / null` |  |
| `error[分支1].code` | 是 | `枚举 INVALID_REQUEST, SCHEMA_VERSION_UNSUPPORTED, CONTENT_VERSION_MISMATCH, AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, POLICY_DENIED, NOT_FOUND, PAYLOAD_TOO_LARGE, IDEMPOTENCY_KEY_REUSED, WORLD_REVISION_CONFLICT, EVENT_SEQUENCE_GAP, SKILL_NOT_CERTIFIED, SKILL_VERSION_MISMATCH, ACTIVE_SKILL_ARTIFACT_MISMATCH, SANDBOX_COMPILE_ERROR, SANDBOX_RUNTIME_ERROR, SANDBOX_RESOURCE_LIMIT, WORLD_RULE_REJECTED, DEPENDENCY_UNAVAILABLE, FEISHU_SIGNATURE_INVALID, FEISHU_REPLAY_DETECTED, FEISHU_SYNC_FAILED, RATE_LIMITED, UNKNOWN_COMMIT_STATE, INVARIANT_VIOLATION, INTERNAL_ERROR` |  |
| `error[分支1].category` | 是 | `枚举 VALIDATION, AUTHENTICATION, AUTHORIZATION, POLICY, CONCURRENCY, SKILL, SANDBOX, WORLD_RULE, DEPENDENCY, INVARIANT, RATE_LIMIT, INTERNAL` |  |
| `error[分支1].retryable` | 是 | `boolean` |  |
| `error[分支1].user_message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `error[分支1].stage` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `error[分支1].message` | 否 | `string; minLength=1; maxLength=512` |  |
| `error[分支1].details` | 否 | `object` |  |
| `error[分支1].evidence_ids` | 否 | `array; maxItems=64` |  |
| `evidence_refs` | 是 | `array; maxItems=64` |  |
| `evidence_refs[].evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `evidence_refs[].evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `evidence_refs[].created_at` | 是 | `string; format=date-time` |  |
| `evidence_refs[].sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `evidence_refs[].uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `versions` | 是 | `object; 不接受额外字段` | Reproducibility versions pinned for one request or execution. |
| `links` | 是 | `object; 不接受额外字段` |  |
| `links.self` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.run` | 否 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.world_snapshot` | 否 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |

完整 JSON 示例：[examples/game-command.json](examples/game-command.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getRun

**读取运行结果** · `GET /v1/runs/{run_id}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `run_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

请求体：无。

### 响应

**HTTP 200**：Run evidence; sandbox success alone never means world commit

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/run.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `run_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` | Agent session that owns this run, or null for a non-Agent invocation. |
| `turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` | Client-supplied turn that owns this run, or null for a non-Agent invocation. |
| `command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `status` | 是 | `枚举 QUEUED, RUNNING_SANDBOX, APPLYING_WORLD, SUCCEEDED, REJECTED, FAILED, UNKNOWN` |  |
| `terminal` | 是 | `boolean` |  |
| `skill` | 是 | `object; 不接受额外字段` |  |
| `skill.skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill.skill_version_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill.artifact_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `skill.certification_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `sandbox` | 是 | `object; 不接受额外字段` |  |
| `sandbox.invocation_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `sandbox.status` | 是 | `枚举 QUEUED, RUNNING, SUCCEEDED, REJECTED, TIMED_OUT, FAILED` |  |
| `sandbox.started_at` | 是 | `string; format=date-time / null` |  |
| `sandbox.finished_at` | 是 | `string; format=date-time / null` |  |
| `sandbox.limits` | 是 | `object; 不接受额外字段` |  |
| `sandbox.limits.cpu_ms` | 是 | `integer; minimum=1` |  |
| `sandbox.limits.wall_ms` | 是 | `integer; minimum=1` |  |
| `sandbox.limits.memory_bytes` | 是 | `integer; minimum=1` |  |
| `sandbox.limits.max_intents` | 是 | `integer; minimum=1` |  |
| `sandbox.usage` | 是 | `object; 不接受额外字段 / null` |  |
| `sandbox.usage[分支1].cpu_ms` | 是 | `integer; minimum=0` |  |
| `sandbox.usage[分支1].wall_ms` | 是 | `integer; minimum=0` |  |
| `sandbox.usage[分支1].peak_memory_bytes` | 是 | `integer; minimum=0` |  |
| `sandbox.action_intents` | 是 | `array` |  |
| `sandbox.failure` | 是 | `object / null` |  |
| `sandbox.failure[分支1].code` | 是 | `枚举 INVALID_REQUEST, SCHEMA_VERSION_UNSUPPORTED, CONTENT_VERSION_MISMATCH, AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, POLICY_DENIED, NOT_FOUND, PAYLOAD_TOO_LARGE, IDEMPOTENCY_KEY_REUSED, WORLD_REVISION_CONFLICT, EVENT_SEQUENCE_GAP, SKILL_NOT_CERTIFIED, SKILL_VERSION_MISMATCH, ACTIVE_SKILL_ARTIFACT_MISMATCH, SANDBOX_COMPILE_ERROR, SANDBOX_RUNTIME_ERROR, SANDBOX_RESOURCE_LIMIT, WORLD_RULE_REJECTED, DEPENDENCY_UNAVAILABLE, FEISHU_SIGNATURE_INVALID, FEISHU_REPLAY_DETECTED, FEISHU_SYNC_FAILED, RATE_LIMITED, UNKNOWN_COMMIT_STATE, INVARIANT_VIOLATION, INTERNAL_ERROR` |  |
| `sandbox.failure[分支1].category` | 是 | `枚举 VALIDATION, AUTHENTICATION, AUTHORIZATION, POLICY, CONCURRENCY, SKILL, SANDBOX, WORLD_RULE, DEPENDENCY, INVARIANT, RATE_LIMIT, INTERNAL` |  |
| `sandbox.failure[分支1].retryable` | 是 | `boolean` |  |
| `sandbox.failure[分支1].user_message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `sandbox.failure[分支1].stage` | 是 | `枚举 SANDBOX, WORLD_VALIDATE, WORLD_COMMIT; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `sandbox.failure[分支1].message` | 否 | `string; minLength=1; maxLength=512` |  |
| `sandbox.failure[分支1].details` | 否 | `object` |  |
| `sandbox.failure[分支1].evidence_ids` | 否 | `array; maxItems=64` |  |
| `world_application` | 是 | `object; 不接受额外字段` |  |
| `world_application.status` | 是 | `枚举 NOT_ATTEMPTED, VALIDATING, COMMITTED, REJECTED, FAILED, UNKNOWN` |  |
| `world_application.receipt` | 是 | `object; 不接受额外字段 / null` |  |
| `world_application.receipt[分支1].world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `world_application.receipt[分支1].previous_revision` | 是 | `integer; minimum=0` |  |
| `world_application.receipt[分支1].world_revision` | 是 | `integer; minimum=1` |  |
| `world_application.receipt[分支1].first_event_sequence` | 是 | `integer; minimum=1` |  |
| `world_application.receipt[分支1].last_event_sequence` | 是 | `integer; minimum=1` |  |
| `world_application.receipt[分支1].state_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `world_application.receipt[分支1].committed_at` | 是 | `string; format=date-time` |  |
| `world_application.failure` | 是 | `object / null` |  |
| `world_application.failure[分支1].code` | 是 | `枚举 INVALID_REQUEST, SCHEMA_VERSION_UNSUPPORTED, CONTENT_VERSION_MISMATCH, AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, POLICY_DENIED, NOT_FOUND, PAYLOAD_TOO_LARGE, IDEMPOTENCY_KEY_REUSED, WORLD_REVISION_CONFLICT, EVENT_SEQUENCE_GAP, SKILL_NOT_CERTIFIED, SKILL_VERSION_MISMATCH, ACTIVE_SKILL_ARTIFACT_MISMATCH, SANDBOX_COMPILE_ERROR, SANDBOX_RUNTIME_ERROR, SANDBOX_RESOURCE_LIMIT, WORLD_RULE_REJECTED, DEPENDENCY_UNAVAILABLE, FEISHU_SIGNATURE_INVALID, FEISHU_REPLAY_DETECTED, FEISHU_SYNC_FAILED, RATE_LIMITED, UNKNOWN_COMMIT_STATE, INVARIANT_VIOLATION, INTERNAL_ERROR` |  |
| `world_application.failure[分支1].category` | 是 | `枚举 VALIDATION, AUTHENTICATION, AUTHORIZATION, POLICY, CONCURRENCY, SKILL, SANDBOX, WORLD_RULE, DEPENDENCY, INVARIANT, RATE_LIMIT, INTERNAL` |  |
| `world_application.failure[分支1].retryable` | 是 | `boolean` |  |
| `world_application.failure[分支1].user_message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `world_application.failure[分支1].stage` | 是 | `枚举 SANDBOX, WORLD_VALIDATE, WORLD_COMMIT; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `world_application.failure[分支1].message` | 否 | `string; minLength=1; maxLength=512` |  |
| `world_application.failure[分支1].details` | 否 | `object` |  |
| `world_application.failure[分支1].evidence_ids` | 否 | `array; maxItems=64` |  |
| `agent_feedback` | 是 | `object; 不接受额外字段 / null` | Reconciled, user-displayable feedback for the Agent turn that produced this run. Null until feedback exists or when this run is not associated with an Agent turn. |
| `agent_feedback[分支1].session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` | Agent session from POST /v1/agent-sessions/{session_id}/turns. |
| `agent_feedback[分支1].turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` | Client-supplied turn_id from the accepted turn request. |
| `agent_feedback[分支1].command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` | Accepted EXECUTE_AGENT_TURN command. This value MUST equal the containing event envelope command_id. |
| `agent_feedback[分支1].run_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` | Sandbox run created for this turn, or null when the turn produced feedback without starting a run. |
| `agent_feedback[分支1].message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` | Stable localization and telemetry key. |
| `agent_feedback[分支1].message` | 是 | `string; minLength=1; maxLength=4000` | Already policy-filtered text that the game client may display to the learner. |
| `agent_feedback[分支1].source` | 是 | `枚举 provider, provider_fallback` |  |
| `agent_feedback[分支1].degraded` | 是 | `boolean` |  |
| `agent_feedback[分支1].fallback_reason` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,95}$ / null` |  |
| `agent_feedback[分支1].evidence_refs` | 是 | `array; maxItems=64` |  |
| `agent_feedback[分支1].evidence_refs[].evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `agent_feedback[分支1].evidence_refs[].evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `agent_feedback[分支1].evidence_refs[].created_at` | 是 | `string; format=date-time` |  |
| `agent_feedback[分支1].evidence_refs[].sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `agent_feedback[分支1].evidence_refs[].uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `agent_feedback[分支1].completed_at` | 是 | `string; format=date-time` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `evidence_refs` | 是 | `array; maxItems=64` |  |
| `evidence_refs[].evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `evidence_refs[].evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `evidence_refs[].created_at` | 是 | `string; format=date-time` |  |
| `evidence_refs[].sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `evidence_refs[].uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `versions` | 是 | `object; 不接受额外字段` | Reproducibility versions pinned for one request or execution. |

完整 JSON 示例：[examples/game-run.json](examples/game-run.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getEvidence

**读取原始证据** · `GET /v1/evidence/{evidence_id}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `evidence_id` | path | 是 | string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$ |

请求体：无。

### 响应

**HTTP 200**：Immutable evidence with integrity hashes

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`ETag`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/evidence.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `evidence_ref` | 是 | `object; 不接受额外字段` |  |
| `evidence_ref.evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `evidence_ref.evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `evidence_ref.created_at` | 是 | `string; format=date-time` |  |
| `evidence_ref.sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `evidence_ref.uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `subject` | 是 | `object; 不接受额外字段` |  |
| `subject.learner_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `source` | 是 | `object; 不接受额外字段` |  |
| `source.source_type` | 是 | `枚举 SKILL_BUILD, SKILL_RUN, WORLD, CLIENT_EVENT, POLICY, LEARNER_PROJECTOR` |  |
| `source.source_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `source.command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$ / null` |  |
| `source.world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` |  |
| `occurred_at` | 是 | `string; format=date-time` |  |
| `recorded_at` | 是 | `string; format=date-time` |  |
| `integrity` | 是 | `object; 不接受额外字段` |  |
| `integrity.payload_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` | SHA-256 of payload encoded using contracts/canonical-json-v1.json. |
| `integrity.previous_evidence_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$ / null` |  |
| `payload` | 是 | `object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段` |  |
| `payload[分支1].evidence_kind` | 是 | `固定 "BUILD_CERTIFICATION"` |  |
| `payload[分支1].build_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `payload[分支1].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `payload[分支1].skill_version_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `payload[分支1].artifact_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `payload[分支1].test_suite_version` | 是 | `string; minLength=1; maxLength=96` |  |
| `payload[分支1].outcome` | 是 | `枚举 CERTIFIED, REJECTED` |  |
| `payload[分支2].evidence_kind` | 是 | `固定 "SKILL_RUN"` |  |
| `payload[分支2].run_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `payload[分支2].sandbox_status` | 是 | `枚举 SUCCEEDED, REJECTED, TIMED_OUT, FAILED` |  |
| `payload[分支2].world_status` | 是 | `枚举 NOT_ATTEMPTED, COMMITTED, REJECTED, FAILED, UNKNOWN` |  |
| `payload[分支2].intent_count` | 是 | `integer; minimum=0` |  |
| `payload[分支3].evidence_kind` | 是 | `固定 "WORLD_COMMIT"` |  |
| `payload[分支3].world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `payload[分支3].previous_revision` | 是 | `integer; minimum=0` |  |
| `payload[分支3].world_revision` | 是 | `integer; minimum=1` |  |
| `payload[分支3].first_event_sequence` | 是 | `integer; minimum=1` |  |
| `payload[分支3].last_event_sequence` | 是 | `integer; minimum=1` |  |
| `payload[分支3].state_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `payload[分支4].evidence_kind` | 是 | `固定 "LEARNER_OBSERVATION"` |  |
| `payload[分支4].observation_type` | 是 | `枚举 CODE_ATTEMPT, DEBUG_ATTEMPT, TASK_COMPLETION, HINT_USE` |  |
| `payload[分支4].task_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `payload[分支4].outcome` | 是 | `枚举 SUCCESS, PARTIAL, FAILED` |  |
| `payload[分支4].assistance_level` | 是 | `integer; minimum=0; maximum=10` |  |
| `related_evidence` | 是 | `array; maxItems=64` |  |
| `related_evidence[].evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `related_evidence[].evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `related_evidence[].created_at` | 是 | `string; format=date-time` |  |
| `related_evidence[].sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `related_evidence[].uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `versions` | 是 | `object; 不接受额外字段` | Reproducibility versions pinned for one request or execution. |

完整 JSON 示例：[examples/game-evidence.json](examples/game-evidence.json)。

### 编译拒绝证据分支

实际路由在 `payload.evidence_kind=BUILD_REJECTION` 时返回以下专用结构；前端按此分支解析，不能套用运行证据 payload。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/build-rejection-evidence.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `evidence_ref` | 是 | `object; 不接受额外字段` |  |
| `evidence_ref.evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `evidence_ref.evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `evidence_ref.created_at` | 是 | `string; format=date-time` |  |
| `evidence_ref.sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `evidence_ref.uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `subject` | 是 | `object; 不接受额外字段` |  |
| `subject.learner_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `source` | 是 | `object; 不接受额外字段` |  |
| `source.source_type` | 是 | `固定 "SKILL_BUILD"` |  |
| `source.source_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `source.command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `source.world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` |  |
| `occurred_at` | 是 | `string; format=date-time` |  |
| `recorded_at` | 是 | `string; format=date-time` |  |
| `integrity` | 是 | `object; 不接受额外字段` |  |
| `integrity.payload_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `integrity.previous_evidence_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$ / null` |  |
| `payload` | 是 | `object; 不接受额外字段` |  |
| `payload.evidence_kind` | 是 | `固定 "BUILD_REJECTION"` |  |
| `payload.build_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `payload.skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `payload.test_suite_version` | 是 | `string; minLength=1; maxLength=96` |  |
| `payload.outcome` | 是 | `固定 "REJECTED"` |  |
| `payload.failure_stage` | 是 | `string; minLength=1; maxLength=64` |  |
| `payload.failure_code` | 是 | `string; minLength=1; maxLength=96` |  |
| `payload.diagnostic_codes` | 是 | `array; maxItems=64` |  |
| `related_evidence` | 是 | `array; maxItems=64` |  |
| `related_evidence[].evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `related_evidence[].evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `related_evidence[].created_at` | 是 | `string; format=date-time` |  |
| `related_evidence[].sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `related_evidence[].uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `versions` | 是 | `object; 不接受额外字段` | Reproducibility versions pinned for one request or execution. |

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getWorldSnapshot

**读取世界快照** · `GET /v1/worlds/{world_id}/snapshot`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `world_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

请求体：无。

### 响应

**HTTP 200**：Authoritative world snapshot

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`ETag`、`X-World-Revision`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/world-snapshot.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `revision` | 是 | `integer; minimum=0` |  |
| `last_event_sequence` | 是 | `integer; minimum=0` |  |
| `state_schema_version` | 是 | `固定 "1.0.0"` |  |
| `state_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `generated_at` | 是 | `string; format=date-time` |  |
| `world_rules_version` | 是 | `string; minLength=1; maxLength=96` |  |
| `state` | 是 | `object; 不接受额外字段` |  |
| `state.clock` | 是 | `object; 不接受额外字段` |  |
| `state.clock.day` | 是 | `integer; minimum=1` |  |
| `state.clock.minute_of_day` | 是 | `integer; minimum=0; maximum=1439` |  |
| `state.clock.tick` | 是 | `integer; minimum=0` |  |
| `state.avatar` | 是 | `object; 不接受额外字段` |  |
| `state.avatar.entity_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `state.avatar.position` | 是 | `object; 不接受额外字段` |  |
| `state.avatar.energy` | 是 | `integer; minimum=0; maximum=10000` |  |
| `state.inventory` | 是 | `array; maxItems=1000` |  |
| `state.inventory[].item_id` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{1,63}$` |  |
| `state.inventory[].quantity` | 是 | `integer; minimum=0; maximum=1000000` |  |
| `state.plots` | 是 | `array; maxItems=10000` |  |
| `state.plots[].plot_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `state.plots[].position` | 是 | `object; 不接受额外字段` |  |
| `state.plots[].soil_state` | 是 | `枚举 UNTILLED, TILLED` |  |
| `state.plots[].hydration` | 是 | `integer; minimum=0; maximum=10000` |  |
| `state.plots[].crop` | 是 | `object; 不接受额外字段 / null` |  |
| `state.plots[].last_updated_event_sequence` | 是 | `integer; minimum=0` |  |
| `state.agents` | 是 | `array; maxItems=256` |  |
| `state.agents[].entity_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `state.agents[].agent_profile_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `state.agents[].position` | 是 | `object; 不接受额外字段` |  |
| `state.agents[].activity` | 是 | `枚举 IDLE, THINKING, EXECUTING, BLOCKED` |  |

完整 JSON 示例：[examples/game-world-snapshot.json](examples/game-world-snapshot.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## listWorldEvents

**读取世界事件** · `GET /v1/worlds/{world_id}/events`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `world_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |
| `after_sequence` | query | 是 | integer; minimum=0 |
| `limit` | query | 否 | integer; minimum=1; maximum=500; default=100 |

请求体：无。

### 响应

**HTTP 200**：Contiguous ordered event page

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`X-World-Revision`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/world-event-page.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `snapshot_revision` | 是 | `integer; minimum=0` |  |
| `from_sequence` | 是 | `integer; minimum=0` |  |
| `to_sequence` | 是 | `integer; minimum=0` |  |
| `has_more` | 是 | `boolean` |  |
| `next_after_sequence` | 是 | `integer; minimum=0` |  |
| `events` | 是 | `array; maxItems=500` |  |
| `events[].event_id` | 是 | `string; pattern=^evt_[A-Za-z0-9_-]{8,128}$` |  |
| `events[].event_type` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `events[].event_version` | 是 | `integer; minimum=1` |  |
| `events[].schema_version` | 是 | `固定 "1.0.0"` |  |
| `events[].stream_id` | 是 | `string; pattern=^[A-Za-z][A-Za-z0-9:_-]{2,159}$` |  |
| `events[].sequence` | 是 | `integer; minimum=1` |  |
| `events[].occurred_at` | 是 | `string; format=date-time` |  |
| `events[].producer` | 是 | `string; pattern=^[a-z][a-z0-9_-]{2,63}$` |  |
| `events[].trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |
| `events[].command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `events[].correlation_id` | 是 | `string; pattern=^corr_[A-Za-z0-9_-]{8,96}$` |  |
| `events[].causation_id` | 是 | `string; pattern=^(?:evt\|cmd)_[A-Za-z0-9_-]{8,128}$ / null` |  |
| `events[].content_ref` | 是 | `object; 不接受额外字段` |  |
| `events[].content_ref.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `events[].content_ref.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `events[].content_ref.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `events[].payload` | 是 | `object` |  |

完整 JSON 示例：[examples/game-world-event-page.json](examples/game-world-event-page.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## listWorldPresentationEvents

**读取世界动画事件** · `GET /v1/worlds/{world_id}/presentation-events`

启用条件：WALNUT_ENABLE_WORLD_PRESENTATION=true。

合同来源：[OpenAPI](../../../agent/contracts/openapi/int2-world-presentation.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `world_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |
| `after_sequence` | query | 是 | integer; minimum=0 |
| `limit` | query | 否 | integer; minimum=1; maximum=500; default=100 |

请求体：无。

### 响应

**HTTP 200**：A closed page from the independent authoritative presentation stream.

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/world-presentation-event-page.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `snapshot_revision` | 是 | `integer; minimum=0` |  |
| `snapshot_last_event_sequence` | 是 | `integer; minimum=0` |  |
| `snapshot_state_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `presentation_high_watermark` | 是 | `integer; minimum=0` |  |
| `from_sequence` | 是 | `integer; minimum=0` |  |
| `to_sequence` | 是 | `integer; minimum=0` |  |
| `has_more` | 是 | `boolean` |  |
| `next_after_sequence` | 是 | `integer; minimum=0` |  |
| `events` | 是 | `array; maxItems=500` |  |
| `events[].event_id` | 是 | `string; pattern=^presentation_[a-f0-9]{32}$` |  |
| `events[].event_type` | 是 | `固定 "world.action.harvested"` |  |
| `events[].event_version` | 是 | `固定 1` |  |
| `events[].schema_version` | 是 | `固定 "1.0.0"` |  |
| `events[].stream_id` | 是 | `string; pattern=^world-presentation:[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].sequence` | 是 | `integer; minimum=1` |  |
| `events[].occurred_at` | 是 | `string; format=date-time` |  |
| `events[].producer` | 是 | `固定 "walnut_world_engine"` |  |
| `events[].tenant_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `events[].run_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].commit_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].world_revision` | 是 | `integer; minimum=1` |  |
| `events[].action_index` | 是 | `integer; minimum=0` |  |
| `events[].action_count` | 是 | `integer; minimum=1; maximum=10000` |  |
| `events[].intent_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].state_hash_before` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `events[].state_hash_after` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `events[].final_snapshot_revision` | 是 | `integer; minimum=1` |  |
| `events[].final_world_event_sequence` | 是 | `integer; minimum=1` |  |
| `events[].final_snapshot_state_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `events[].payload` | 是 | `object; 不接受额外字段` |  |
| `events[].payload.actor_entity_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].payload.plot_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].payload.position` | 是 | `object; 不接受额外字段` |  |
| `events[].payload.crop_type` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{1,63}$` |  |
| `events[].payload.growth_stage` | 是 | `integer; minimum=0; maximum=100` |  |
| `events[].payload.ready_to_harvest` | 是 | `固定 true` |  |
| `events[].payload_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `events[].integrity_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |

完整 JSON 示例：[examples/game-world-presentation-event-page.json](examples/game-world-presentation-event-page.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getStudentBootstrap

**学生启动信息** · `GET /v1/student-bootstrap`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/student-bootstrap-v2.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |

请求体：无。

### 响应

**HTTP 200**：Student launch, build, activation, session, and world recovery authority

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/student-bootstrap-v2.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `api_version` | 是 | `固定 "1.1.0"` |  |
| `contract_version` | 是 | `固定 "0.4.0"` |  |
| `server_time` | 是 | `string; format=date-time` |  |
| `actor` | 是 | `object; 不接受额外字段` |  |
| `actor.tenant_id` | 是 | `string; pattern=^[A-Za-z0-9_-]{3,96}$` |  |
| `actor.actor_id` | 是 | `string; pattern=^[A-Za-z0-9_-]{3,128}$` |  |
| `actor.actor_type` | 是 | `枚举 student, agent, teacher, researcher, operator, service` |  |
| `actor.roles` | 是 | `array; maxItems=16` |  |
| `content` | 是 | `object; 不接受额外字段` |  |
| `content.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `capabilities` | 是 | `object; 不接受额外字段` |  |
| `capabilities.skill_builds` | 是 | `boolean` |  |
| `capabilities.skill_activations` | 是 | `boolean` |  |
| `capabilities.agent_sessions` | 是 | `boolean` |  |
| `capabilities.http_world_recovery` | 是 | `boolean` |  |
| `capabilities.evidence_query` | 是 | `boolean` |  |
| `session` | 是 | `object; 不接受额外字段` |  |
| `session.current_session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` |  |
| `session.teaching_spec_version` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` |  |
| `session.create_request` | 是 | `object; 不接受额外字段` |  |
| `session.create_request.world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session.create_request.learner_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session.create_request.agent_profile_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session.create_request.channel` | 是 | `固定 "GAME"` |  |
| `session.create_request.locale` | 是 | `string; pattern=^[a-z]{2,3}(?:-[A-Z]{2})?$` |  |
| `session.create_request.content` | 是 | `object; 不接受额外字段` |  |
| `session.create_request.expected_world_revision` | 是 | `integer; minimum=0` |  |
| `build` | 是 | `object; 不接受额外字段` |  |
| `build.build_policy_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` |  |
| `build.compiler_profile` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` |  |
| `build.compiler_version` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` |  |
| `build.sandbox_image_digest` | 是 | `string; pattern=^sha256:[a-f0-9]{64}$` |  |
| `build.test_suite_version` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` |  |
| `build.allowed_capabilities` | 是 | `array; maxItems=7` |  |
| `build.max_source_files` | 是 | `固定 32` | Maximum number of files in one SkillSourceBundle. |
| `build.max_source_bytes` | 是 | `固定 1048576` | Maximum UTF-8 content bytes in one SkillSourceBundle. |
| `activation` | 是 | `object; 不接受额外字段` |  |
| `activation.scope` | 是 | `object; 不接受额外字段` |  |
| `activation.scope.world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `activation.scope.agent_profile_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `activation.registry_revision` | 是 | `integer; minimum=0` |  |
| `activation.active` | 是 | `object; 不接受额外字段 / null` |  |
| `activation.active[分支1].activation_id` | 是 | `string; pattern=^activation_[A-Za-z0-9_-]{8,118}$` |  |
| `activation.active[分支1].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `activation.active[分支1].skill_version_id` | 是 | `string; pattern=^skillver_[A-Za-z0-9_-]{8,118}$` |  |
| `activation.active[分支1].artifact_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `activation.active[分支1].certification_id` | 是 | `string; pattern=^cert_[A-Za-z0-9_-]{8,122}$` |  |
| `activation.active[分支1].registry_revision` | 是 | `integer; minimum=1` |  |
| `activation.active[分支1].activated_at` | 是 | `string; format=date-time` |  |
| `world` | 是 | `object; 不接受额外字段` |  |
| `world.world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `world.revision` | 是 | `integer; minimum=0` |  |
| `world.last_event_sequence` | 是 | `integer; minimum=0` |  |
| `world.state_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `world.snapshot_url` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `world.events_url` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |

完整 JSON 示例：[examples/game-student-bootstrap-v2.json](examples/game-student-bootstrap-v2.json)。

合同声明的错误状态：400、401、403、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## queryLearnerProjectionFromFeishu

**教师查询学生学习记录** · `POST /integrations/feishu/v1/learner-queries`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/feishu-integration.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `Idempotency-Key` | header | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$ |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |

### JSON 请求体

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/feishu/learner-query.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `context.schema_version` | 是 | `固定 "1.0.0"` |  |
| `context.request_id` | 是 | `string; pattern=^req_[A-Za-z0-9_-]{8,96}$` |  |
| `context.correlation_id` | 是 | `string; pattern=^corr_[A-Za-z0-9_-]{8,96}$` |  |
| `context.trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |
| `context.requested_at` | 是 | `string; format=date-time` |  |
| `context.actor` | 是 | `object; 不接受额外字段` |  |
| `context.actor.tenant_id` | 是 | `string; pattern=^[A-Za-z0-9_-]{3,96}$` |  |
| `context.actor.actor_id` | 是 | `string; pattern=^[A-Za-z0-9_-]{3,128}$` |  |
| `context.actor.actor_type` | 是 | `枚举 student, agent, teacher, researcher, operator, service` |  |
| `context.actor.roles` | 是 | `array; maxItems=16` |  |
| `context.content_ref` | 是 | `object; 不接受额外字段` |  |
| `context.content_ref.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `context.content_ref.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `context.content_ref.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `learner_ref` | 是 | `string; pattern=^lrn_[A-Za-z0-9_-]{8,128}$` | Tenant-scoped opaque learner reference; raw names, phone numbers and email addresses are forbidden. |
| `purpose` | 是 | `枚举 TEACHER_SUPPORT, GUARDIAN_REPORT, LEARNING_REVIEW, SAFETY_INVESTIGATION` |  |
| `requested_fields` | 是 | `array; minItems=1; maxItems=6` |  |
| `consent_basis` | 是 | `枚举 EDUCATIONAL_SERVICE, GUARDIAN_CONSENT, LEGAL_SAFETY_DUTY` |  |
| `time_range` | 否 | `object; 不接受额外字段` |  |
| `time_range.from` | 是 | `string; format=date-time` |  |
| `time_range.to` | 是 | `string; format=date-time` |  |

完整 JSON 示例：[examples/feishu-learner-query.json](examples/feishu-learner-query.json)。

### 响应

**HTTP 200**：Redacted learner projection and explicit projection freshness.

响应头：`X-Request-Id`、`X-Trace-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/feishu/learner-query-result.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `query_id` | 是 | `string; pattern=^lqry_[A-Za-z0-9_-]{8,128}$` |  |
| `learner_ref` | 是 | `string; pattern=^lrn_[A-Za-z0-9_-]{8,128}$` |  |
| `as_of` | 是 | `string; format=date-time` |  |
| `data_freshness` | 是 | `object; 不接受额外字段` |  |
| `data_freshness.projected_through_event_at` | 是 | `string; format=date-time` |  |
| `data_freshness.projection_lag_seconds` | 是 | `integer; minimum=0` |  |
| `data_freshness.is_stale` | 否 | `boolean` |  |
| `mastery_summary` | 否 | `array; maxItems=500` |  |
| `mastery_summary[].concept_ref` | 是 | `string; minLength=1; maxLength=256` |  |
| `mastery_summary[].state` | 是 | `枚举 NOT_OBSERVED, EMERGING, DEVELOPING, PROFICIENT, REVIEW_NEEDED` |  |
| `mastery_summary[].confidence` | 是 | `number; minimum=0; maximum=1` |  |
| `mastery_summary[].evidence_refs` | 是 | `array; maxItems=100` |  |
| `mastery_summary[].evidence_refs[].evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `mastery_summary[].evidence_refs[].evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `mastery_summary[].evidence_refs[].created_at` | 是 | `string; format=date-time` |  |
| `mastery_summary[].evidence_refs[].sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `mastery_summary[].evidence_refs[].uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `mastery_summary[].updated_at` | 是 | `string; format=date-time` |  |
| `recent_evidence` | 否 | `array; maxItems=200` |  |
| `recent_evidence[].evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `recent_evidence[].evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `recent_evidence[].created_at` | 是 | `string; format=date-time` |  |
| `recent_evidence[].sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `recent_evidence[].uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `support_needs` | 否 | `array; maxItems=100` |  |
| `activity_summary` | 否 | `object; 不接受额外字段` |  |
| `activity_summary.sessions` | 否 | `integer; minimum=0` |  |
| `activity_summary.completed_tasks` | 否 | `integer; minimum=0` |  |
| `activity_summary.active_minutes` | 否 | `integer; minimum=0` |  |
| `recommended_next_steps` | 否 | `array; maxItems=20` |  |
| `redaction` | 是 | `object; 不接受额外字段` |  |
| `redaction.direct_identifiers_removed` | 是 | `固定 true` |  |
| `redaction.fields_omitted` | 是 | `array` |  |
| `trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |

完整 JSON 示例：[examples/feishu-learner-query-result.json](examples/feishu-learner-query-result.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## queryClassInsightsFromFeishu

**教师查询班级统计** · `POST /integrations/feishu/v1/class-insights`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/feishu-integration.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `Idempotency-Key` | header | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$ |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |

### JSON 请求体

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/feishu/class-insights-query.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `context.schema_version` | 是 | `固定 "1.0.0"` |  |
| `context.request_id` | 是 | `string; pattern=^req_[A-Za-z0-9_-]{8,96}$` |  |
| `context.correlation_id` | 是 | `string; pattern=^corr_[A-Za-z0-9_-]{8,96}$` |  |
| `context.trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |
| `context.requested_at` | 是 | `string; format=date-time` |  |
| `context.actor` | 是 | `object; 不接受额外字段` |  |
| `context.actor.tenant_id` | 是 | `string; pattern=^[A-Za-z0-9_-]{3,96}$` |  |
| `context.actor.actor_id` | 是 | `string; pattern=^[A-Za-z0-9_-]{3,128}$` |  |
| `context.actor.actor_type` | 是 | `枚举 student, agent, teacher, researcher, operator, service` |  |
| `context.actor.roles` | 是 | `array; maxItems=16` |  |
| `context.content_ref` | 是 | `object; 不接受额外字段` |  |
| `context.content_ref.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `context.content_ref.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `context.content_ref.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `class_ref` | 是 | `string; pattern=^cls_[A-Za-z0-9_-]{8,128}$` |  |
| `purpose` | 是 | `枚举 TEACHER_PLANNING, CURRICULUM_REVIEW, PROGRAM_EVALUATION` |  |
| `time_range` | 是 | `object; 不接受额外字段` |  |
| `time_range.from` | 是 | `string; format=date-time` |  |
| `time_range.to` | 是 | `string; format=date-time` |  |
| `dimensions` | 是 | `array; minItems=1; maxItems=5` |  |
| `privacy` | 是 | `object; 不接受额外字段` |  |
| `privacy.minimum_cohort_size` | 是 | `integer; minimum=5; maximum=1000` |  |
| `privacy.suppress_small_cells` | 是 | `固定 true` |  |

完整 JSON 示例：[examples/feishu-class-insights-query.json](examples/feishu-class-insights-query.json)。

### 响应

**HTTP 200**：Aggregate result with explicit suppression metadata and no learner identifiers.

响应头：`X-Request-Id`、`X-Trace-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/feishu/class-insights-result.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `query_id` | 是 | `string; pattern=^ciq_[A-Za-z0-9_-]{8,128}$` |  |
| `class_ref` | 是 | `string; pattern=^cls_[A-Za-z0-9_-]{8,128}$` |  |
| `as_of` | 是 | `string; format=date-time` |  |
| `cohort_size` | 是 | `integer; minimum=0` |  |
| `privacy` | 是 | `object; 不接受额外字段` |  |
| `privacy.minimum_cohort_size` | 是 | `integer; minimum=5` |  |
| `privacy.effective_minimum_cohort_size` | 是 | `integer; minimum=5; maximum=1000` | Actual server-enforced threshold; it may be higher than the caller-requested minimum. |
| `privacy.policy_version` | 是 | `string; minLength=1; maxLength=128` |  |
| `privacy.small_cells_suppressed` | 是 | `固定 true` |  |
| `privacy.contains_learner_identifiers` | 是 | `固定 false` |  |
| `insights` | 是 | `array; maxItems=500` |  |
| `insights[].dimension` | 是 | `枚举 CONCEPT_MASTERY, COMMON_ERRORS, SUPPORT_NEEDS, ENGAGEMENT, COMPLETION` |  |
| `insights[].key` | 是 | `string; minLength=1; maxLength=256` |  |
| `insights[].learner_count` | 是 | `integer; minimum=0 / null` |  |
| `insights[].ratio` | 是 | `number; minimum=0; maximum=1 / null` |  |
| `insights[].suppressed` | 是 | `boolean` |  |
| `trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |

完整 JSON 示例：[examples/feishu-class-insights-result.json](examples/feishu-class-insights-result.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getRedactedEvidenceForFeishu

**教师读取脱敏证据** · `GET /integrations/feishu/v1/evidence/{evidence_id}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/feishu-integration.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `evidence_id` | path | 是 | string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$ |
| `purpose` | query | 是 | 枚举 TEACHER_SUPPORT, GUARDIAN_REPORT, LEARNING_REVIEW, SAFETY_INVESTIGATION |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |

请求体：无。

### 响应

**HTTP 200**：Redacted, read-only evidence projection with provenance.

响应头：`X-Request-Id`、`X-Trace-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/feishu/evidence-view.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `evidence_ref` | 是 | `object; 不接受额外字段` |  |
| `evidence_ref.evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `evidence_ref.evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `evidence_ref.created_at` | 是 | `string; format=date-time` |  |
| `evidence_ref.sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `evidence_ref.uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `learner_ref` | 是 | `string; pattern=^lrn_[A-Za-z0-9_-]{8,128}$` |  |
| `observed_at` | 是 | `string; format=date-time` |  |
| `summary` | 是 | `string; maxLength=4000` |  |
| `facts` | 是 | `array; maxItems=200` |  |
| `facts[].name` | 是 | `string; minLength=1; maxLength=128` |  |
| `facts[].value` | 是 | `string; maxLength=2000 / number / boolean` |  |
| `provenance` | 是 | `object; 不接受额外字段` |  |
| `provenance.event_id` | 是 | `string; minLength=1; maxLength=128` |  |
| `provenance.command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `provenance.source_module` | 是 | `枚举 SKILL_LIFECYCLE, WORLD_ENGINE, AGENT_RUNTIME, LEARNER_PROJECTION` |  |
| `redaction` | 是 | `object; 不接受额外字段` |  |
| `redaction.direct_identifiers_removed` | 是 | `固定 true` |  |
| `redaction.policy_version` | 是 | `string; minLength=1; maxLength=128` |  |
| `redaction.fields_omitted` | 是 | `array` |  |
| `trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |

完整 JSON 示例：[examples/feishu-evidence-view.json](examples/feishu-evidence-view.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## feishuTeacherMcp

**教师只读 MCP 工具** · `POST /integrations/feishu/v1/mcp`

启用条件：路由始终挂载。

采用 JSON-RPC 2.0；详见 [教师与可选接口](补充接口.md#mcp)。

### query_learner_progress

查询已脱敏的学生掌握、活动、Evidence、支持需求和建议事实。只需提供 learner_ref；content_ref 仅在多内容时用于消歧。只读；不接受姓名、租户、角色或任意用途。

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "learner_ref"
  ],
  "properties": {
    "learner_ref": {
      "type": "string",
      "pattern": "^lrn_[A-Za-z0-9_-]{8,128}$"
    },
    "content_ref": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "unit_id",
        "version",
        "content_hash"
      ],
      "properties": {
        "unit_id": {
          "type": "string",
          "pattern": "^[A-Z0-9][A-Z0-9_-]{2,79}$"
        },
        "version": {
          "type": "string",
          "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+(?:-[0-9A-Za-z.-]+)?$"
        },
        "content_hash": {
          "type": "string",
          "pattern": "^[a-f0-9]{64}$"
        }
      }
    },
    "time_range": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "from",
        "to"
      ],
      "properties": {
        "from": {
          "type": "string",
          "format": "date-time",
          "maxLength": 64
        },
        "to": {
          "type": "string",
          "format": "date-time",
          "maxLength": 64
        }
      }
    }
  }
}
```

### query_class_common_issues

查询班级知识点、高频错误、支持需求、活跃和完成分布。class_ref、content_ref、time_range 均可省略，由 Backend 从已认证租户、唯一 Learner Profile 内容和上海时区最近 7 个自然日解析。小样本单元由 Backend 强制抑制，不返回学生标识。

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "class_ref": {
      "type": "string",
      "pattern": "^cls_[A-Za-z0-9_-]{8,128}$"
    },
    "content_ref": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "unit_id",
        "version",
        "content_hash"
      ],
      "properties": {
        "unit_id": {
          "type": "string",
          "pattern": "^[A-Z0-9][A-Z0-9_-]{2,79}$"
        },
        "version": {
          "type": "string",
          "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+(?:-[0-9A-Za-z.-]+)?$"
        },
        "content_hash": {
          "type": "string",
          "pattern": "^[a-f0-9]{64}$"
        }
      }
    },
    "time_range": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "from",
        "to"
      ],
      "properties": {
        "from": {
          "type": "string",
          "format": "date-time",
          "maxLength": 64
        },
        "to": {
          "type": "string",
          "format": "date-time",
          "maxLength": 64
        }
      }
    }
  }
}
```

### get_evidence_summary_and_links

使用查询学生学习进度返回的 recent_evidence.evidence_id，查看该 Evidence 的白名单事实、来源和脱敏声明；不得猜测或自行构造 evidence_id。并返回受信任的教师工作台学生详情入口（可在页内打开成长档案）与 Dashboard 链接；不伪装成直达云文档链接。

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "evidence_id"
  ],
  "properties": {
    "evidence_id": {
      "type": "string",
      "pattern": "^evidence_[A-Za-z0-9_-]{8,120}$"
    }
  }
}
```


## createSkillBuild

**提交编译** · `POST /v1/skill-builds`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `Idempotency-Key` | header | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$ |

### JSON 请求体

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/skill-build-create-request.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `display_name` | 是 | `string; minLength=1; maxLength=80` |  |
| `client_draft_revision` | 是 | `integer; minimum=0` |  |
| `source_bundle` | 是 | `object; 不接受额外字段` |  |
| `source_bundle.language` | 是 | `固定 "CPP20"` |  |
| `source_bundle.entrypoint` | 是 | `string; pattern=^[A-Za-z0-9_.\/-]{1,240}$` |  |
| `source_bundle.files` | 是 | `array; minItems=1; maxItems=32` |  |
| `source_bundle.files[].path` | 是 | `string; pattern=^(?!/)(?!.*(?:^\|/)\.\.(?:/\|$))[A-Za-z0-9_.\/-]{1,240}$` |  |
| `source_bundle.files[].content` | 是 | `string; maxLength=1048576` |  |
| `source_bundle.files[].content_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `compiler_profile` | 是 | `枚举 YAYA_CPP20_SAFE_V1` |  |
| `test_suite_version` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` |  |
| `requested_capabilities` | 否 | `array; maxItems=16; default=[]` |  |

完整 JSON 示例：[examples/game-skill-build-create-request.json](examples/game-skill-build-create-request.json)。

### 响应

**HTTP 202**：Durably accepted for asynchronous processing; this is not an execution-success response. AcceptedGameJob.trace_id remains the original command trace, while X-Trace-Id identifies the current HTTP attempt.

响应头：`Location`、`Retry-After`、`Idempotency-Replayed`、`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/accepted-game-job.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `job_id` | 是 | `string; pattern=^job_[A-Za-z0-9_-]{8,96}$` |  |
| `job_type` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `status` | 是 | `枚举 ACCEPTED, QUEUED` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |
| `error` | 是 | `object; 不接受额外字段 / null` |  |
| `error[分支1].code` | 是 | `枚举 INVALID_REQUEST, SCHEMA_VERSION_UNSUPPORTED, CONTENT_VERSION_MISMATCH, AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, POLICY_DENIED, NOT_FOUND, PAYLOAD_TOO_LARGE, IDEMPOTENCY_KEY_REUSED, WORLD_REVISION_CONFLICT, EVENT_SEQUENCE_GAP, SKILL_NOT_CERTIFIED, SKILL_VERSION_MISMATCH, ACTIVE_SKILL_ARTIFACT_MISMATCH, SANDBOX_COMPILE_ERROR, SANDBOX_RUNTIME_ERROR, SANDBOX_RESOURCE_LIMIT, WORLD_RULE_REJECTED, DEPENDENCY_UNAVAILABLE, FEISHU_SIGNATURE_INVALID, FEISHU_REPLAY_DETECTED, FEISHU_SYNC_FAILED, RATE_LIMITED, UNKNOWN_COMMIT_STATE, INVARIANT_VIOLATION, INTERNAL_ERROR` |  |
| `error[分支1].category` | 是 | `枚举 VALIDATION, AUTHENTICATION, AUTHORIZATION, POLICY, CONCURRENCY, SKILL, SANDBOX, WORLD_RULE, DEPENDENCY, INVARIANT, RATE_LIMIT, INTERNAL` |  |
| `error[分支1].retryable` | 是 | `boolean` |  |
| `error[分支1].user_message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `error[分支1].stage` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `error[分支1].message` | 否 | `string; minLength=1; maxLength=512` |  |
| `error[分支1].details` | 否 | `object` |  |
| `error[分支1].evidence_ids` | 否 | `array; maxItems=64` |  |

合同声明的错误状态：400、401、403、409、413、422、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getSkillBuild

**读取编译结果** · `GET /v1/skill-builds/{build_id}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `build_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

请求体：无。

### 响应

**HTTP 200**：Current skill build resource

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/skill-build.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `build_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_version_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` |  |
| `status` | 是 | `枚举 ACCEPTED, QUEUED, COMPILING, TESTING, CERTIFYING, CERTIFIED, REJECTED, FAILED` |  |
| `terminal` | 是 | `boolean` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `artifact` | 是 | `object; 不接受额外字段 / null` |  |
| `artifact[分支1].artifact_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `artifact[分支1].source_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `artifact[分支1].compiler_profile` | 是 | `string; minLength=1; maxLength=64` |  |
| `artifact[分支1].compiler_version` | 是 | `string; minLength=1; maxLength=64` |  |
| `artifact[分支1].test_suite_version` | 是 | `string; minLength=1; maxLength=64` |  |
| `certification` | 是 | `object; 不接受额外字段 / null` |  |
| `certification[分支1].certification_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `certification[分支1].issued_at` | 是 | `string; format=date-time` |  |
| `certification[分支1].capabilities` | 是 | `array` |  |
| `phases` | 是 | `array; minItems=1` |  |
| `phases[].name` | 是 | `枚举 VALIDATE_SOURCE, COMPILE, PUBLIC_TEST, HIDDEN_TEST, CERTIFY` |  |
| `phases[].status` | 是 | `枚举 PENDING, RUNNING, PASSED, FAILED, SKIPPED` |  |
| `phases[].started_at` | 否 | `string; format=date-time / null` |  |
| `phases[].finished_at` | 否 | `string; format=date-time / null` |  |
| `phases[].diagnostic_codes` | 否 | `array; maxItems=100` |  |
| `failure` | 是 | `object / null` |  |
| `failure[分支1].code` | 是 | `枚举 INVALID_REQUEST, SCHEMA_VERSION_UNSUPPORTED, CONTENT_VERSION_MISMATCH, AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, POLICY_DENIED, NOT_FOUND, PAYLOAD_TOO_LARGE, IDEMPOTENCY_KEY_REUSED, WORLD_REVISION_CONFLICT, EVENT_SEQUENCE_GAP, SKILL_NOT_CERTIFIED, SKILL_VERSION_MISMATCH, ACTIVE_SKILL_ARTIFACT_MISMATCH, SANDBOX_COMPILE_ERROR, SANDBOX_RUNTIME_ERROR, SANDBOX_RESOURCE_LIMIT, WORLD_RULE_REJECTED, DEPENDENCY_UNAVAILABLE, FEISHU_SIGNATURE_INVALID, FEISHU_REPLAY_DETECTED, FEISHU_SYNC_FAILED, RATE_LIMITED, UNKNOWN_COMMIT_STATE, INVARIANT_VIOLATION, INTERNAL_ERROR` |  |
| `failure[分支1].category` | 是 | `枚举 VALIDATION, AUTHENTICATION, AUTHORIZATION, POLICY, CONCURRENCY, SKILL, SANDBOX, WORLD_RULE, DEPENDENCY, INVARIANT, RATE_LIMIT, INTERNAL` |  |
| `failure[分支1].retryable` | 是 | `boolean` |  |
| `failure[分支1].user_message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `failure[分支1].stage` | 是 | `枚举 VALIDATE_SOURCE, COMPILE, PUBLIC_TEST, HIDDEN_TEST, CERTIFY; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `failure[分支1].message` | 否 | `string; minLength=1; maxLength=512` |  |
| `failure[分支1].details` | 否 | `object` |  |
| `failure[分支1].evidence_ids` | 否 | `array; maxItems=64` |  |
| `evidence_refs` | 是 | `array` |  |
| `evidence_refs[].evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `evidence_refs[].evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `evidence_refs[].created_at` | 是 | `string; format=date-time` |  |
| `evidence_refs[].sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `evidence_refs[].uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `versions` | 是 | `object; 不接受额外字段` | Reproducibility versions pinned for one request or execution. |

完整 JSON 示例：[examples/game-skill-build.json](examples/game-skill-build.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## activateSkillVersion

**激活技能版本** · `POST /v1/skill-versions/{skill_version_id}/activations`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `Idempotency-Key` | header | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$ |
| `skill_version_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

### JSON 请求体

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/skill-activation-request.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `expected_registry_revision` | 是 | `integer; minimum=0` |  |
| `activation_scope` | 是 | `object; 不接受额外字段` |  |
| `activation_scope.world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `activation_scope.agent_profile_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `reason` | 否 | `string; minLength=1; maxLength=500` |  |

完整 JSON 示例：[examples/game-skill-activation-request.json](examples/game-skill-activation-request.json)。

### 响应

**HTTP 202**：Durably accepted for asynchronous processing; this is not an execution-success response. AcceptedGameJob.trace_id remains the original command trace, while X-Trace-Id identifies the current HTTP attempt.

响应头：`Location`、`Retry-After`、`Idempotency-Replayed`、`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/accepted-game-job.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `job_id` | 是 | `string; pattern=^job_[A-Za-z0-9_-]{8,96}$` |  |
| `job_type` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `status` | 是 | `枚举 ACCEPTED, QUEUED` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |
| `error` | 是 | `object; 不接受额外字段 / null` |  |
| `error[分支1].code` | 是 | `枚举 INVALID_REQUEST, SCHEMA_VERSION_UNSUPPORTED, CONTENT_VERSION_MISMATCH, AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, POLICY_DENIED, NOT_FOUND, PAYLOAD_TOO_LARGE, IDEMPOTENCY_KEY_REUSED, WORLD_REVISION_CONFLICT, EVENT_SEQUENCE_GAP, SKILL_NOT_CERTIFIED, SKILL_VERSION_MISMATCH, ACTIVE_SKILL_ARTIFACT_MISMATCH, SANDBOX_COMPILE_ERROR, SANDBOX_RUNTIME_ERROR, SANDBOX_RESOURCE_LIMIT, WORLD_RULE_REJECTED, DEPENDENCY_UNAVAILABLE, FEISHU_SIGNATURE_INVALID, FEISHU_REPLAY_DETECTED, FEISHU_SYNC_FAILED, RATE_LIMITED, UNKNOWN_COMMIT_STATE, INVARIANT_VIOLATION, INTERNAL_ERROR` |  |
| `error[分支1].category` | 是 | `枚举 VALIDATION, AUTHENTICATION, AUTHORIZATION, POLICY, CONCURRENCY, SKILL, SANDBOX, WORLD_RULE, DEPENDENCY, INVARIANT, RATE_LIMIT, INTERNAL` |  |
| `error[分支1].retryable` | 是 | `boolean` |  |
| `error[分支1].user_message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `error[分支1].stage` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `error[分支1].message` | 否 | `string; minLength=1; maxLength=512` |  |
| `error[分支1].details` | 否 | `object` |  |
| `error[分支1].evidence_ids` | 否 | `array; maxItems=64` |  |

合同声明的错误状态：400、401、403、404、409、422、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getSkillActivation

**读取激活结果** · `GET /v1/skill-activations/{activation_id}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `activation_id` | path | 是 | string; pattern=^activation_[A-Za-z0-9_-]{8,118}$ |

请求体：无。

### 响应

**HTTP 200**：Materialized skill activation

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/skill-activation.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `activation_id` | 是 | `string; pattern=^activation_[A-Za-z0-9_-]{8,118}$` |  |
| `skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_version_id` | 是 | `string; pattern=^skillver_[A-Za-z0-9_-]{8,118}$` |  |
| `certification_id` | 是 | `string; pattern=^cert_[A-Za-z0-9_-]{8,122}$` |  |
| `artifact_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `activation_scope` | 是 | `object; 不接受额外字段` |  |
| `activation_scope.world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `activation_scope.agent_profile_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `previous_registry_revision` | 是 | `integer; minimum=0` |  |
| `registry_revision` | 是 | `integer; minimum=1` |  |
| `activated_at` | 是 | `string; format=date-time` |  |

完整 JSON 示例：[examples/game-skill-activation.json](examples/game-skill-activation.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## createAgentSession

**创建会话** · `POST /v1/agent-sessions`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `Idempotency-Key` | header | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$ |

### JSON 请求体

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/agent-session-create-request.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `learner_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `agent_profile_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `channel` | 是 | `枚举 GAME, TEACHER_PREVIEW` |  |
| `locale` | 是 | `string; pattern=^[a-z]{2,3}(?:-[A-Z]{2})?$` |  |
| `content` | 是 | `object; 不接受额外字段` |  |
| `content.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `expected_world_revision` | 否 | `integer; minimum=0` |  |

完整 JSON 示例：[examples/game-agent-session-create-request.json](examples/game-agent-session-create-request.json)。

### 响应

**HTTP 202**：Durably accepted for asynchronous processing; this is not an execution-success response. AcceptedGameJob.trace_id remains the original command trace, while X-Trace-Id identifies the current HTTP attempt.

响应头：`Location`、`Retry-After`、`Idempotency-Replayed`、`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/accepted-game-job.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `job_id` | 是 | `string; pattern=^job_[A-Za-z0-9_-]{8,96}$` |  |
| `job_type` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `status` | 是 | `枚举 ACCEPTED, QUEUED` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |
| `error` | 是 | `object; 不接受额外字段 / null` |  |
| `error[分支1].code` | 是 | `枚举 INVALID_REQUEST, SCHEMA_VERSION_UNSUPPORTED, CONTENT_VERSION_MISMATCH, AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, POLICY_DENIED, NOT_FOUND, PAYLOAD_TOO_LARGE, IDEMPOTENCY_KEY_REUSED, WORLD_REVISION_CONFLICT, EVENT_SEQUENCE_GAP, SKILL_NOT_CERTIFIED, SKILL_VERSION_MISMATCH, ACTIVE_SKILL_ARTIFACT_MISMATCH, SANDBOX_COMPILE_ERROR, SANDBOX_RUNTIME_ERROR, SANDBOX_RESOURCE_LIMIT, WORLD_RULE_REJECTED, DEPENDENCY_UNAVAILABLE, FEISHU_SIGNATURE_INVALID, FEISHU_REPLAY_DETECTED, FEISHU_SYNC_FAILED, RATE_LIMITED, UNKNOWN_COMMIT_STATE, INVARIANT_VIOLATION, INTERNAL_ERROR` |  |
| `error[分支1].category` | 是 | `枚举 VALIDATION, AUTHENTICATION, AUTHORIZATION, POLICY, CONCURRENCY, SKILL, SANDBOX, WORLD_RULE, DEPENDENCY, INVARIANT, RATE_LIMIT, INTERNAL` |  |
| `error[分支1].retryable` | 是 | `boolean` |  |
| `error[分支1].user_message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `error[分支1].stage` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `error[分支1].message` | 否 | `string; minLength=1; maxLength=512` |  |
| `error[分支1].details` | 否 | `object` |  |
| `error[分支1].evidence_ids` | 否 | `array; maxItems=64` |  |

合同声明的错误状态：400、401、403、409、422、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getAgentSession

**读取会话与 Turn 序号** · `GET /v1/agent-sessions/{session_id}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `session_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

请求体：无。

### 响应

**HTTP 200**：Agent session resource

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/agent-session.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `learner_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `agent_profile_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `channel` | 是 | `枚举 GAME, TEACHER_PREVIEW` |  |
| `status` | 是 | `枚举 ACTIVE, CLOSING, CLOSED, FAILED` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `last_turn_sequence` | 是 | `integer; minimum=0` |  |
| `content` | 是 | `object; 不接受额外字段` |  |
| `content.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `versions` | 是 | `object; 不接受额外字段` | Reproducibility versions pinned for one request or execution. |
| `links` | 是 | `object; 不接受额外字段` |  |
| `links.self` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.turns` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.world_snapshot` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |

完整 JSON 示例：[examples/game-agent-session.json](examples/game-agent-session.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## createAgentTurn

**运行技能／文字提问／请求代码建议** · `POST /v1/agent-sessions/{session_id}/turns`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `Idempotency-Key` | header | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$ |
| `session_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

### JSON 请求体

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/agent-turn-create-request.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `expected_world_revision` | 是 | `integer; minimum=0` |  |
| `input` | 是 | `object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段` |  |
| `input[分支1].type` | 是 | `固定 "MESSAGE"` |  |
| `input[分支1].text` | 是 | `string; minLength=1; maxLength=4000` |  |
| `input[分支1].locale` | 是 | `string; pattern=^[a-z]{2,3}(?:-[A-Z]{2})?$` |  |
| `input[分支2].type` | 是 | `固定 "ASSIGNED_TASK"` |  |
| `input[分支2].task_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `input[分支3].type` | 是 | `固定 "UI_ACTION"` |  |
| `input[分支3].action_id` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{1,95}$` |  |
| `input[分支3].selection_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_bindings` | 是 | `array; maxItems=32` |  |
| `skill_bindings[].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_bindings[].skill_version_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_bindings[].artifact_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `skill_bindings[].certification_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `client_state` | 是 | `object; 不接受额外字段` |  |
| `client_state.last_event_sequence` | 是 | `integer; minimum=0` |  |
| `client_state.client_turn_sequence` | 是 | `integer; minimum=1` |  |

完整 JSON 示例：[examples/game-agent-turn-create-request.json](examples/game-agent-turn-create-request.json)。

### 响应

**HTTP 202**：Durably accepted for asynchronous processing; this is not an execution-success response. AcceptedGameJob.trace_id remains the original command trace, while X-Trace-Id identifies the current HTTP attempt.

响应头：`Location`、`Retry-After`、`Idempotency-Replayed`、`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/accepted-game-job.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `job_id` | 是 | `string; pattern=^job_[A-Za-z0-9_-]{8,96}$` |  |
| `job_type` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `status` | 是 | `枚举 ACCEPTED, QUEUED` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |
| `error` | 是 | `object; 不接受额外字段 / null` |  |
| `error[分支1].code` | 是 | `枚举 INVALID_REQUEST, SCHEMA_VERSION_UNSUPPORTED, CONTENT_VERSION_MISMATCH, AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, POLICY_DENIED, NOT_FOUND, PAYLOAD_TOO_LARGE, IDEMPOTENCY_KEY_REUSED, WORLD_REVISION_CONFLICT, EVENT_SEQUENCE_GAP, SKILL_NOT_CERTIFIED, SKILL_VERSION_MISMATCH, ACTIVE_SKILL_ARTIFACT_MISMATCH, SANDBOX_COMPILE_ERROR, SANDBOX_RUNTIME_ERROR, SANDBOX_RESOURCE_LIMIT, WORLD_RULE_REJECTED, DEPENDENCY_UNAVAILABLE, FEISHU_SIGNATURE_INVALID, FEISHU_REPLAY_DETECTED, FEISHU_SYNC_FAILED, RATE_LIMITED, UNKNOWN_COMMIT_STATE, INVARIANT_VIOLATION, INTERNAL_ERROR` |  |
| `error[分支1].category` | 是 | `枚举 VALIDATION, AUTHENTICATION, AUTHORIZATION, POLICY, CONCURRENCY, SKILL, SANDBOX, WORLD_RULE, DEPENDENCY, INVARIANT, RATE_LIMIT, INTERNAL` |  |
| `error[分支1].retryable` | 是 | `boolean` |  |
| `error[分支1].user_message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `error[分支1].stage` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `error[分支1].message` | 否 | `string; minLength=1; maxLength=512` |  |
| `error[分支1].details` | 否 | `object` |  |
| `error[分支1].evidence_ids` | 否 | `array; maxItems=64` |  |

合同声明的错误状态：400、401、403、404、409、422、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## ingestClientEventBatch

**批量上报客户端事件** · `POST /v1/client-events:batch`

启用条件：WALNUT_ENABLE_CLIENT_EVENT_BATCH=true。

合同来源：[OpenAPI](../../../agent/contracts/openapi/game-api.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `Idempotency-Key` | header | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$ |

### JSON 请求体

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/client-event-batch-request.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `batch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `first_sequence` | 是 | `integer; minimum=1` |  |
| `last_sequence` | 是 | `integer; minimum=1` |  |
| `events` | 是 | `array; minItems=1; maxItems=500` |  |
| `events[].event_id` | 是 | `string; pattern=^client_evt_[A-Za-z0-9_-]{8,128}$` |  |
| `events[].sequence` | 是 | `integer; minimum=1` |  |
| `events[].occurred_at` | 是 | `string; format=date-time` |  |
| `events[].event_type` | 是 | `枚举 UI_ACTION, CODE_EDITED, BUILD_REQUESTED, HINT_VIEWED, FEEDBACK_SHOWN, ANIMATION_COMPLETED, CLIENT_ERROR, SESSION_HEARTBEAT` |  |
| `events[].world_revision` | 是 | `integer; minimum=0` |  |
| `events[].payload` | 是 | `object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段 / object; 不接受额外字段` |  |
| `events[].payload[分支1].action_id` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{1,95}$` |  |
| `events[].payload[分支1].component_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_.-]{1,95}$` |  |
| `events[].payload[分支2].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].payload[分支2].draft_revision` | 是 | `integer; minimum=0` |  |
| `events[].payload[分支2].source_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `events[].payload[分支2].changed_file_count` | 是 | `integer; minimum=1; maximum=64` |  |
| `events[].payload[分支3].build_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].payload[分支3].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].payload[分支4].hint_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `events[].payload[分支4].hint_level` | 是 | `integer; minimum=1; maximum=10` |  |
| `events[].payload[分支5].message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `events[].payload[分支5].source` | 是 | `枚举 AGENT, POLICY, SYSTEM` |  |
| `events[].payload[分支5].degraded` | 是 | `boolean` |  |
| `events[].payload[分支6].action_event_id` | 是 | `string; pattern=^evt_[A-Za-z0-9_-]{8,128}$` |  |
| `events[].payload[分支6].animation_id` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{1,95}$` |  |
| `events[].payload[分支6].result` | 是 | `枚举 COMPLETED, SKIPPED, FAILED` |  |
| `events[].payload[分支7].code` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,95}$` |  |
| `events[].payload[分支7].message_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `events[].payload[分支7].screen` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{1,95}$` |  |
| `events[].payload[分支7].handled` | 是 | `boolean` |  |
| `events[].payload[分支8].last_received_event_sequence` | 是 | `integer; minimum=0` |  |

完整 JSON 示例：[examples/game-client-event-batch-request.json](examples/game-client-event-batch-request.json)。

### 响应

**HTTP 202**：Durably accepted for asynchronous processing; this is not an execution-success response. AcceptedGameJob.trace_id remains the original command trace, while X-Trace-Id identifies the current HTTP attempt.

响应头：`Location`、`Retry-After`、`Idempotency-Replayed`、`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/game/accepted-game-job.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `job_id` | 是 | `string; pattern=^job_[A-Za-z0-9_-]{8,96}$` |  |
| `job_type` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `status` | 是 | `枚举 ACCEPTED, QUEUED` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` |  |
| `trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |
| `error` | 是 | `object; 不接受额外字段 / null` |  |
| `error[分支1].code` | 是 | `枚举 INVALID_REQUEST, SCHEMA_VERSION_UNSUPPORTED, CONTENT_VERSION_MISMATCH, AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, POLICY_DENIED, NOT_FOUND, PAYLOAD_TOO_LARGE, IDEMPOTENCY_KEY_REUSED, WORLD_REVISION_CONFLICT, EVENT_SEQUENCE_GAP, SKILL_NOT_CERTIFIED, SKILL_VERSION_MISMATCH, ACTIVE_SKILL_ARTIFACT_MISMATCH, SANDBOX_COMPILE_ERROR, SANDBOX_RUNTIME_ERROR, SANDBOX_RESOURCE_LIMIT, WORLD_RULE_REJECTED, DEPENDENCY_UNAVAILABLE, FEISHU_SIGNATURE_INVALID, FEISHU_REPLAY_DETECTED, FEISHU_SYNC_FAILED, RATE_LIMITED, UNKNOWN_COMMIT_STATE, INVARIANT_VIOLATION, INTERNAL_ERROR` |  |
| `error[分支1].category` | 是 | `枚举 VALIDATION, AUTHENTICATION, AUTHORIZATION, POLICY, CONCURRENCY, SKILL, SANDBOX, WORLD_RULE, DEPENDENCY, INVARIANT, RATE_LIMIT, INTERNAL` |  |
| `error[分支1].retryable` | 是 | `boolean` |  |
| `error[分支1].user_message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `error[分支1].stage` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `error[分支1].message` | 否 | `string; minLength=1; maxLength=512` |  |
| `error[分支1].details` | 否 | `object` |  |
| `error[分支1].evidence_ids` | 否 | `array; maxItems=64` |  |

合同声明的错误状态：400、401、403、409、413、422、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getProductSkillDraft

**读取代码草稿** · `GET /product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/product-experience.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `session_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |
| `draft_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

请求体：无。

### 响应

**HTTP 200**：Current canonical SkillDraft.

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`ETag`、`X-Draft-Revision`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/skill-draft.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `revision` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `content_ref` | 是 | `object; 不接受额外字段` |  |
| `content_ref.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content_ref.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content_ref.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `display_name` | 是 | `string; minLength=1; maxLength=80` |  |
| `source_bundle` | 是 | `object` | The existing Game SkillSourceBundle narrowed to host-portable canonical logical paths for recoverable product drafts and patches. |
| `source_bundle.language` | 是 | `固定 "CPP20"` |  |
| `source_bundle.entrypoint` | 是 | `string; pattern=^[A-Za-z0-9_.\/-]{1,240}$` |  |
| `source_bundle.files` | 是 | `array; minItems=1; maxItems=32` |  |
| `source_bundle.files[].path` | 否 | `string; pattern=^(?=.{1,240}$)[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?(?:/[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)*$` |  |
| `draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` | Server-computed hash of the frozen draft identity projection named by x-draft-hash-contract. Clients echo it for CAS; they never choose it. |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `last_applied_patch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` |  |
| `links` | 是 | `object; 不接受额外字段` |  |
| `links.self` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.session_workspace` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.builds` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |

完整 JSON 示例：[examples/product-experience-skill-draft.json](examples/product-experience-skill-draft.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## upsertProductSkillDraft

**保存代码草稿** · `PUT /product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/product-experience.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `Idempotency-Key` | header | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$ |
| `session_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |
| `draft_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

### JSON 请求体

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/skill-draft-upsert-request.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `content_ref` | 是 | `object; 不接受额外字段` |  |
| `content_ref.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content_ref.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content_ref.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `base_revision` | 是 | `integer; minimum=0; maximum=9007199254740991` |  |
| `base_draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$ / null` |  |
| `display_name` | 是 | `string; minLength=1; maxLength=80` |  |
| `source_bundle` | 是 | `object` | The existing Game SkillSourceBundle narrowed to host-portable canonical logical paths for recoverable product drafts and patches. |
| `source_bundle.language` | 是 | `固定 "CPP20"` |  |
| `source_bundle.entrypoint` | 是 | `string; pattern=^[A-Za-z0-9_.\/-]{1,240}$` |  |
| `source_bundle.files` | 是 | `array; minItems=1; maxItems=32` |  |
| `source_bundle.files[].path` | 否 | `string; pattern=^(?=.{1,240}$)[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?(?:/[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)*$` |  |
| `client_saved_at` | 是 | `string; format=date-time` |  |

完整 JSON 示例：[examples/product-experience-skill-draft-upsert-request.json](examples/product-experience-skill-draft-upsert-request.json)。

### 响应

**HTTP 200**：Draft replaced by successful revision-and-hash CAS. Location is its canonical GET URL.

响应头：`Location`、`Idempotency-Replayed`、`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`ETag`、`X-Draft-Revision`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/skill-draft.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `revision` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `content_ref` | 是 | `object; 不接受额外字段` |  |
| `content_ref.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content_ref.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content_ref.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `display_name` | 是 | `string; minLength=1; maxLength=80` |  |
| `source_bundle` | 是 | `object` | The existing Game SkillSourceBundle narrowed to host-portable canonical logical paths for recoverable product drafts and patches. |
| `source_bundle.language` | 是 | `固定 "CPP20"` |  |
| `source_bundle.entrypoint` | 是 | `string; pattern=^[A-Za-z0-9_.\/-]{1,240}$` |  |
| `source_bundle.files` | 是 | `array; minItems=1; maxItems=32` |  |
| `source_bundle.files[].path` | 否 | `string; pattern=^(?=.{1,240}$)[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?(?:/[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)*$` |  |
| `draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` | Server-computed hash of the frozen draft identity projection named by x-draft-hash-contract. Clients echo it for CAS; they never choose it. |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `last_applied_patch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` |  |
| `links` | 是 | `object; 不接受额外字段` |  |
| `links.self` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.session_workspace` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.builds` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |

完整 JSON 示例：[examples/product-experience-skill-draft.json](examples/product-experience-skill-draft.json)。

**HTTP 201**：Draft created at revision 1. Location is its canonical GET URL.

响应头：`Location`、`Idempotency-Replayed`、`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`ETag`、`X-Draft-Revision`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/skill-draft.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `revision` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `content_ref` | 是 | `object; 不接受额外字段` |  |
| `content_ref.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content_ref.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content_ref.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `display_name` | 是 | `string; minLength=1; maxLength=80` |  |
| `source_bundle` | 是 | `object` | The existing Game SkillSourceBundle narrowed to host-portable canonical logical paths for recoverable product drafts and patches. |
| `source_bundle.language` | 是 | `固定 "CPP20"` |  |
| `source_bundle.entrypoint` | 是 | `string; pattern=^[A-Za-z0-9_.\/-]{1,240}$` |  |
| `source_bundle.files` | 是 | `array; minItems=1; maxItems=32` |  |
| `source_bundle.files[].path` | 否 | `string; pattern=^(?=.{1,240}$)[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?(?:/[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)*$` |  |
| `draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` | Server-computed hash of the frozen draft identity projection named by x-draft-hash-contract. Clients echo it for CAS; they never choose it. |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `last_applied_patch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` |  |
| `links` | 是 | `object; 不接受额外字段` |  |
| `links.self` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.session_workspace` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.builds` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |

完整 JSON 示例：[examples/product-experience-skill-draft.json](examples/product-experience-skill-draft.json)。

合同声明的错误状态：400、401、403、404、409、413、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getProductContentUnit

**读取关卡内容** · `GET /product-experience/v1/content-units/{unit_id}/versions/{content_version}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/product-experience.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `unit_id` | path | 是 | string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$ |
| `content_version` | path | 是 | string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$ |
| `content_hash` | query | 是 | string; pattern=^[a-f0-9]{64}$ |

请求体：无。

### 响应

**HTTP 200**：Exact immutable published content unit.

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`ETag`、`Cache-Control`、`Vary`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/content-unit.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `content_ref` | 是 | `object; 不接受额外字段` |  |
| `content_ref.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content_ref.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content_ref.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `status` | 是 | `固定 "PUBLISHED"` |  |
| `unit_type` | 是 | `固定 "TASK"` |  |
| `audiences` | 是 | `array; minItems=1; maxItems=2` |  |
| `task` | 是 | `object; 不接受额外字段` |  |
| `task.task_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `task.name` | 是 | `string; minLength=1; maxLength=120` |  |
| `task.goal` | 是 | `string; minLength=1; maxLength=1000` |  |
| `task.instructions` | 是 | `array; minItems=1; maxItems=32` |  |
| `task.knowledge_points` | 是 | `array; maxItems=64` |  |
| `task.allowed_capabilities` | 是 | `array; maxItems=16` |  |
| `task.starter_skill` | 是 | `object; 不接受额外字段 / null` |  |
| `task.starter_skill[分支1].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `task.starter_skill[分支1].display_name` | 是 | `string; minLength=1; maxLength=80` |  |
| `task.starter_skill[分支1].source_bundle` | 是 | `object` | The existing Game SkillSourceBundle narrowed to host-portable canonical logical paths for recoverable product drafts and patches. |
| `task.starter_skill[分支1].compiler_profile` | 是 | `固定 "YAYA_CPP20_SAFE_V1"` |  |
| `task.starter_skill[分支1].test_suite_version` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` |  |
| `task.hint_policy` | 是 | `object; 不接受额外字段` |  |
| `task.hint_policy.max_level` | 是 | `固定 4` |  |
| `task.hint_policy.levels` | 是 | `array; minItems=5; maxItems=5` |  |
| `task.story` | 是 | `object; 不接受额外字段` |  |
| `task.story.opening` | 是 | `string; minLength=1; maxLength=2000` |  |
| `task.story.success` | 是 | `string; minLength=1; maxLength=2000` |  |
| `published_at` | 是 | `string; format=date-time` |  |
| `links` | 是 | `object; 不接受额外字段` |  |
| `links.self` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |

完整 JSON 示例：[examples/product-experience-content-unit.json](examples/product-experience-content-unit.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getInt2Capabilities

**查询可用功能** · `GET /product-experience/v1/capabilities`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/int2-product-capabilities.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |

请求体：无。

### 响应

**HTTP 200**：Current authenticated INT2 capability projection.

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/int2-capabilities.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `api_version` | 是 | `固定 "1.2.0"` |  |
| `contract_version` | 是 | `固定 "0.6.0"` |  |
| `world_presentation_enabled` | 是 | `boolean` |  |
| `skill_patch_enabled` | 是 | `boolean` |  |
| `skill_patch_constraints` | 是 | `object; 不接受额外字段` |  |
| `skill_patch_constraints.request_mode` | 是 | `固定 "EXPLICIT_UI_ACTION"` |  |
| `skill_patch_constraints.selection_target` | 是 | `固定 "FAILED_INTERACTION"` |  |
| `skill_patch_constraints.agent_role` | 是 | `固定 "teaching_agent"` |  |
| `skill_patch_constraints.scenario` | 是 | `固定 "RECTIFICATION"` |  |
| `skill_patch_constraints.required_hint_level` | 是 | `固定 4` |  |
| `skill_patch_constraints.operation` | 是 | `固定 "UPSERT_FILE"` |  |
| `skill_patch_constraints.target` | 是 | `固定 "CURRENT_ENTRYPOINT"` |  |
| `skill_patch_constraints.max_files` | 是 | `固定 1` |  |
| `skill_patch_constraints.max_operations` | 是 | `固定 1` |  |
| `skill_patch_constraints.requires_failed_evidence` | 是 | `固定 true` |  |
| `skill_patch_constraints.cas_required` | 是 | `固定 true` |  |
| `skill_patch_constraints.requires_student_confirmation` | 是 | `固定 true` |  |
| `skill_patch_constraints.auto_build` | 是 | `固定 false` |  |
| `skill_patch_constraints.auto_activate` | 是 | `固定 false` |  |
| `skill_patch_constraints.auto_run` | 是 | `固定 false` |  |

完整 JSON 示例：[examples/product-experience-int2-capabilities.json](examples/product-experience-int2-capabilities.json)。

合同声明的错误状态：400、401、403、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## listProductAgentInteractions

**读取反馈／提问／总结列表** · `GET /product-experience/v1/sessions/{session_id}/agent-interactions`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/product-experience.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `session_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |
| `after_sequence` | query | 是 | integer; minimum=0; maximum=9007199254740991 |
| `limit` | query | 否 | integer; minimum=1; maximum=100; default=50 |

请求体：无。

### 响应

**HTTP 200**：Gap-free interaction page after the requested durable sequence.

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`X-Interaction-High-Watermark`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/agent-interaction-page.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `requested_after_sequence` | 是 | `integer; minimum=0; maximum=9007199254740991` |  |
| `requested_limit` | 是 | `integer; minimum=1; maximum=100` |  |
| `high_watermark_sequence` | 是 | `integer; minimum=0; maximum=9007199254740991` |  |
| `from_sequence` | 是 | `integer; minimum=1; maximum=9007199254740991 / null` |  |
| `to_sequence` | 是 | `integer; minimum=1; maximum=9007199254740991 / null` |  |
| `has_more` | 是 | `boolean` |  |
| `next_after_sequence` | 是 | `integer; minimum=0; maximum=9007199254740991` |  |
| `interactions` | 是 | `array; maxItems=100` |  |
| `interactions[].request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `interactions[].interaction_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].sequence` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `interactions[].interaction_revision` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `interactions[].projection_source` | 是 | `object; 不接受额外字段` | Canonical Product projection receipt that makes the structured decision fields independently hash-verifiable without extending AgentTurnFeedback. |
| `interactions[].role` | 是 | `枚举 world_agent, xiaohutao, teaching_agent, bug_agent, book_agent, system` |  |
| `interactions[].response_type` | 是 | `枚举 message, question, hint, skill_patch, growth_summary` |  |
| `interactions[].question` | 是 | `string; minLength=1; maxLength=1000 / null` |  |
| `interactions[].hint_level` | 是 | `integer; minimum=0; maximum=4 / null` |  |
| `interactions[].feedback` | 是 | `object; 不接受额外字段` | Public, user-displayable feedback produced for one agent turn. The containing runtime event supplies ordered delivery; command_id MUST equal the event envelope command_id. |
| `interactions[].feedback.session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` | Agent session from POST /v1/agent-sessions/{session_id}/turns. |
| `interactions[].feedback.turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` | Client-supplied turn_id from the accepted turn request. |
| `interactions[].feedback.command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` | Accepted EXECUTE_AGENT_TURN command. This value MUST equal the containing event envelope command_id. |
| `interactions[].feedback.run_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` | Sandbox run created for this turn, or null when the turn produced feedback without starting a run. |
| `interactions[].feedback.message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` | Stable localization and telemetry key. |
| `interactions[].feedback.message` | 是 | `string; minLength=1; maxLength=4000` | Already policy-filtered text that the game client may display to the learner. |
| `interactions[].feedback.source` | 是 | `枚举 provider, provider_fallback` |  |
| `interactions[].feedback.degraded` | 是 | `boolean` |  |
| `interactions[].feedback.fallback_reason` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,95}$ / null` |  |
| `interactions[].feedback.evidence_refs` | 是 | `array; maxItems=64` |  |
| `interactions[].feedback.completed_at` | 是 | `string; format=date-time` |  |
| `interactions[].feedback_event` | 是 | `object; 不接受额外字段` |  |
| `interactions[].skill_patch` | 是 | `object; 不接受额外字段 / null` |  |
| `interactions[].skill_patch[分支1].patch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].skill_patch[分支1].interaction_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].skill_patch[分支1].session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].skill_patch[分支1].turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].skill_patch[分支1].draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].skill_patch[分支1].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].skill_patch[分支1].base_draft_revision` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `interactions[].skill_patch[分支1].base_draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `interactions[].skill_patch[分支1].operations` | 是 | `array; minItems=1; maxItems=35` |  |
| `interactions[].skill_patch[分支1].result_draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `interactions[].skill_patch[分支1].patch_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` | Server-computed immutable proposal identity; PatchDecision must echo it exactly. |
| `interactions[].skill_patch[分支1].rationale` | 是 | `string; minLength=1; maxLength=2000` |  |
| `interactions[].skill_patch[分支1].requires_student_confirmation` | 是 | `固定 true` |  |
| `interactions[].skill_patch[分支1].evidence_refs` | 是 | `array; maxItems=64` |  |
| `interactions[].skill_patch[分支1].created_at` | 是 | `string; format=date-time` |  |
| `interactions[].patch_decision` | 是 | `object; 不接受额外字段 / null` |  |
| `interactions[].patch_decision[分支1].request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `interactions[].patch_decision[分支1].decision_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].patch_decision[分支1].session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].patch_decision[分支1].turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].patch_decision[分支1].interaction_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].patch_decision[分支1].interaction_revision_before` | 是 | `integer; minimum=1; maximum=9007199254740990` |  |
| `interactions[].patch_decision[分支1].interaction_revision_after` | 是 | `integer; minimum=2; maximum=9007199254740991` |  |
| `interactions[].patch_decision[分支1].patch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].patch_decision[分支1].patch_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `interactions[].patch_decision[分支1].draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].patch_decision[分支1].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interactions[].patch_decision[分支1].decision` | 是 | `枚举 ACCEPT, REJECT` |  |
| `interactions[].patch_decision[分支1].reason_code` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,95}$ / null` |  |
| `interactions[].patch_decision[分支1].draft_updated` | 是 | `boolean` |  |
| `interactions[].patch_decision[分支1].draft_revision_before` | 是 | `integer; minimum=1; maximum=9007199254740990` |  |
| `interactions[].patch_decision[分支1].draft_sha256_before` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `interactions[].patch_decision[分支1].draft_revision_after` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `interactions[].patch_decision[分支1].draft_sha256_after` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `interactions[].patch_decision[分支1].decided_at` | 是 | `string; format=date-time` |  |
| `interactions[].patch_decision[分支1].links` | 是 | `object; 不接受额外字段` |  |
| `interactions[].created_at` | 是 | `string; format=date-time` |  |
| `interactions[].updated_at` | 是 | `string; format=date-time` |  |
| `interactions[].links` | 是 | `object; 不接受额外字段` |  |
| `interactions[].links.self` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `interactions[].links.session_workspace` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `interactions[].links.skill_draft` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048 / null` |  |

完整 JSON 示例：[examples/product-experience-agent-interaction-page.json](examples/product-experience-agent-interaction-page.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getProductAgentInteraction

**读取单条反馈及建议决定** · `GET /product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/product-experience.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `session_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |
| `interaction_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

请求体：无。

### 响应

**HTTP 200**：Canonical interaction including an immutable patch decision when one exists.

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`ETag`、`X-Interaction-Revision`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/agent-interaction.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `interaction_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `sequence` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `interaction_revision` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `projection_source` | 是 | `object; 不接受额外字段` | Canonical Product projection receipt that makes the structured decision fields independently hash-verifiable without extending AgentTurnFeedback. |
| `role` | 是 | `枚举 world_agent, xiaohutao, teaching_agent, bug_agent, book_agent, system` |  |
| `response_type` | 是 | `枚举 message, question, hint, skill_patch, growth_summary` |  |
| `question` | 是 | `string; minLength=1; maxLength=1000 / null` |  |
| `hint_level` | 是 | `integer; minimum=0; maximum=4 / null` |  |
| `feedback` | 是 | `object; 不接受额外字段` | Public, user-displayable feedback produced for one agent turn. The containing runtime event supplies ordered delivery; command_id MUST equal the event envelope command_id. |
| `feedback.session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` | Agent session from POST /v1/agent-sessions/{session_id}/turns. |
| `feedback.turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` | Client-supplied turn_id from the accepted turn request. |
| `feedback.command_id` | 是 | `string; pattern=^cmd_[A-Za-z0-9_-]{8,96}$` | Accepted EXECUTE_AGENT_TURN command. This value MUST equal the containing event envelope command_id. |
| `feedback.run_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ / null` | Sandbox run created for this turn, or null when the turn produced feedback without starting a run. |
| `feedback.message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` | Stable localization and telemetry key. |
| `feedback.message` | 是 | `string; minLength=1; maxLength=4000` | Already policy-filtered text that the game client may display to the learner. |
| `feedback.source` | 是 | `枚举 provider, provider_fallback` |  |
| `feedback.degraded` | 是 | `boolean` |  |
| `feedback.fallback_reason` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,95}$ / null` |  |
| `feedback.evidence_refs` | 是 | `array; maxItems=64` |  |
| `feedback.evidence_refs[].evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `feedback.evidence_refs[].evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `feedback.evidence_refs[].created_at` | 是 | `string; format=date-time` |  |
| `feedback.evidence_refs[].sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `feedback.evidence_refs[].uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `feedback.completed_at` | 是 | `string; format=date-time` |  |
| `feedback_event` | 是 | `object; 不接受额外字段` |  |
| `skill_patch` | 是 | `object; 不接受额外字段 / null` |  |
| `skill_patch[分支1].patch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_patch[分支1].interaction_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_patch[分支1].session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_patch[分支1].turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_patch[分支1].draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_patch[分支1].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_patch[分支1].base_draft_revision` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `skill_patch[分支1].base_draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `skill_patch[分支1].operations` | 是 | `array; minItems=1; maxItems=35` |  |
| `skill_patch[分支1].result_draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `skill_patch[分支1].patch_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` | Server-computed immutable proposal identity; PatchDecision must echo it exactly. |
| `skill_patch[分支1].rationale` | 是 | `string; minLength=1; maxLength=2000` |  |
| `skill_patch[分支1].requires_student_confirmation` | 是 | `固定 true` |  |
| `skill_patch[分支1].evidence_refs` | 是 | `array; maxItems=64` |  |
| `skill_patch[分支1].evidence_refs[].evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `skill_patch[分支1].evidence_refs[].evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `skill_patch[分支1].evidence_refs[].created_at` | 是 | `string; format=date-time` |  |
| `skill_patch[分支1].evidence_refs[].sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `skill_patch[分支1].evidence_refs[].uri` | 否 | `string; minLength=1; maxLength=1024` |  |
| `skill_patch[分支1].created_at` | 是 | `string; format=date-time` |  |
| `patch_decision` | 是 | `object; 不接受额外字段 / null` |  |
| `patch_decision[分支1].request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `patch_decision[分支1].decision_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `patch_decision[分支1].session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `patch_decision[分支1].turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `patch_decision[分支1].interaction_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `patch_decision[分支1].interaction_revision_before` | 是 | `integer; minimum=1; maximum=9007199254740990` |  |
| `patch_decision[分支1].interaction_revision_after` | 是 | `integer; minimum=2; maximum=9007199254740991` |  |
| `patch_decision[分支1].patch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `patch_decision[分支1].patch_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `patch_decision[分支1].draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `patch_decision[分支1].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `patch_decision[分支1].decision` | 是 | `枚举 ACCEPT, REJECT` |  |
| `patch_decision[分支1].reason_code` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,95}$ / null` |  |
| `patch_decision[分支1].draft_updated` | 是 | `boolean` |  |
| `patch_decision[分支1].draft_revision_before` | 是 | `integer; minimum=1; maximum=9007199254740990` |  |
| `patch_decision[分支1].draft_sha256_before` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `patch_decision[分支1].draft_revision_after` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `patch_decision[分支1].draft_sha256_after` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `patch_decision[分支1].decided_at` | 是 | `string; format=date-time` |  |
| `patch_decision[分支1].links` | 是 | `object; 不接受额外字段` |  |
| `patch_decision[分支1].links.interaction` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `patch_decision[分支1].links.skill_draft` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `links` | 是 | `object; 不接受额外字段` |  |
| `links.self` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.session_workspace` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.skill_draft` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048 / null` |  |

完整 JSON 示例：[examples/product-experience-agent-interaction.json](examples/product-experience-agent-interaction.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## recordProductPatchDecision

**接受或拒绝代码建议** · `POST /product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}/patches/{patch_id}/decision`

启用条件：WALNUT_ENABLE_SKILL_PATCH=true（同时要求 WORLD_PRESENTATION）。

合同来源：[OpenAPI](../../../agent/contracts/openapi/product-experience.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `Idempotency-Key` | header | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$ |
| `session_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |
| `interaction_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |
| `patch_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

### JSON 请求体

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/patch-decision-request.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `decision_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interaction_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `expected_interaction_revision` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `patch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `patch_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `base_draft_revision` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `base_draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `result_draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `decision` | 是 | `枚举 ACCEPT, REJECT` |  |
| `reason_code` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,95}$ / null` |  |
| `decided_at` | 是 | `string; format=date-time` |  |

完整 JSON 示例：[examples/product-experience-patch-decision-request.json](examples/product-experience-patch-decision-request.json)。

### 响应

**HTTP 200**：Immutable decision receipt. Location identifies the canonical interaction GET that embeds this receipt.

响应头：`Location`、`Idempotency-Replayed`、`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`ETag`、`X-Interaction-Revision`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/patch-decision-receipt.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `decision_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `turn_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interaction_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `interaction_revision_before` | 是 | `integer; minimum=1; maximum=9007199254740990` |  |
| `interaction_revision_after` | 是 | `integer; minimum=2; maximum=9007199254740991` |  |
| `patch_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `patch_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `decision` | 是 | `枚举 ACCEPT, REJECT` |  |
| `reason_code` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,95}$ / null` |  |
| `draft_updated` | 是 | `boolean` |  |
| `draft_revision_before` | 是 | `integer; minimum=1; maximum=9007199254740990` |  |
| `draft_sha256_before` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `draft_revision_after` | 是 | `integer; minimum=1; maximum=9007199254740991` |  |
| `draft_sha256_after` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `decided_at` | 是 | `string; format=date-time` |  |
| `links` | 是 | `object; 不接受额外字段` |  |
| `links.interaction` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.skill_draft` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |

完整 JSON 示例：[examples/product-experience-patch-decision-receipt.json](examples/product-experience-patch-decision-receipt.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。


## getProductSessionWorkspace

**恢复工作区** · `GET /product-experience/v1/sessions/{session_id}/workspace`

启用条件：路由始终挂载。

合同来源：[OpenAPI](../../../agent/contracts/openapi/product-experience.openapi.json)。

### 请求参数

| 名称 | 位置 | 必填 | 类型／约束 |
| --- | --- | --- | --- |
| `X-Request-Id` | header | 是 | string; pattern=^req_[A-Za-z0-9_-]{8,96}$ |
| `X-Trace-Id` | header | 是 | string; pattern=^trace_[A-Za-z0-9_-]{8,96}$ |
| `X-Correlation-Id` | header | 是 | string; pattern=^corr_[A-Za-z0-9_-]{8,96}$ |
| `X-Schema-Version` | header | 是 | 固定 "1.0.0" |
| `session_id` | path | 是 | string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ |

请求体：无。

### 响应

**HTTP 200**：Current actor-scoped session recovery projection.

响应头：`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`ETag`。

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/product-experience/session-workspace.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `workspace_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `workspace_revision` | 是 | `integer; minimum=1` |  |
| `session` | 是 | `object; 不接受额外字段` |  |
| `session.request_context` | 是 | `object; 不接受额外字段` | Domain context captured when an operation originates a resource. Persisted resources retain this origin context; it is not rewritten to identify a later HTTP polling attempt. Current HTTP attempt identity is carried by the required Game API headers X-Request-Id, X-Trace-Id, X-Correlation-Id, and X-Schema-Version. |
| `session.session_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session.world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session.learner_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session.agent_profile_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `session.channel` | 是 | `枚举 GAME, TEACHER_PREVIEW` |  |
| `session.status` | 是 | `枚举 ACTIVE, CLOSING, CLOSED, FAILED` |  |
| `session.created_at` | 是 | `string; format=date-time` |  |
| `session.updated_at` | 是 | `string; format=date-time` |  |
| `session.last_turn_sequence` | 是 | `integer; minimum=0` |  |
| `session.content` | 是 | `object; 不接受额外字段` |  |
| `session.content.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `session.content.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `session.content.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `session.versions` | 是 | `object; 不接受额外字段` | Reproducibility versions pinned for one request or execution. |
| `session.links` | 是 | `object; 不接受额外字段` |  |
| `session.links.self` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `session.links.turns` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `session.links.world_snapshot` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `content_ref` | 是 | `object; 不接受额外字段` |  |
| `content_ref.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content_ref.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content_ref.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `current_task` | 是 | `object; 不接受额外字段` |  |
| `current_task.task_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `current_task.status` | 是 | `枚举 NOT_STARTED, IN_PROGRESS, COMPLETED, ABANDONED` |  |
| `current_task.started_at` | 是 | `string; format=date-time / null` |  |
| `current_task.completed_at` | 是 | `string; format=date-time / null` |  |
| `world_checkpoint` | 是 | `object; 不接受额外字段` |  |
| `world_checkpoint.world_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `world_checkpoint.world_revision` | 是 | `integer; minimum=0` |  |
| `world_checkpoint.last_event_sequence` | 是 | `integer; minimum=0` |  |
| `world_checkpoint.state_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `skill_draft_refs` | 是 | `array; maxItems=32` |  |
| `skill_draft_refs[].draft_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_draft_refs[].skill_id` | 是 | `string; pattern=^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$` |  |
| `skill_draft_refs[].revision` | 是 | `integer; minimum=1` |  |
| `skill_draft_refs[].draft_sha256` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |
| `skill_draft_refs[].url` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `last_interaction_sequence` | 是 | `integer; minimum=0` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `updated_at` | 是 | `string; format=date-time` |  |
| `links` | 是 | `object; 不接受额外字段` |  |
| `links.self` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.content_unit` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.agent_interactions` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |
| `links.world_snapshot` | 是 | `string; format=uri-reference; minLength=1; maxLength=2048` |  |

完整 JSON 示例：[examples/product-experience-session-workspace.json](examples/product-experience-session-workspace.json)。

合同声明的错误状态：400、401、403、404、409、429、500、503。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。

## 公共结构

### request-context

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/common/request-context.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | 是 | `固定 "1.0.0"` |  |
| `request_id` | 是 | `string; pattern=^req_[A-Za-z0-9_-]{8,96}$` |  |
| `correlation_id` | 是 | `string; pattern=^corr_[A-Za-z0-9_-]{8,96}$` |  |
| `trace_id` | 是 | `string; pattern=^trace_[A-Za-z0-9_-]{8,96}$` |  |
| `requested_at` | 是 | `string; format=date-time` |  |
| `actor` | 是 | `object; 不接受额外字段` |  |
| `actor.tenant_id` | 是 | `string; pattern=^[A-Za-z0-9_-]{3,96}$` |  |
| `actor.actor_id` | 是 | `string; pattern=^[A-Za-z0-9_-]{3,128}$` |  |
| `actor.actor_type` | 是 | `枚举 student, agent, teacher, researcher, operator, service` |  |
| `actor.roles` | 是 | `array; maxItems=16` |  |
| `content_ref` | 是 | `object; 不接受额外字段` |  |
| `content_ref.unit_id` | 是 | `string; pattern=^[A-Z0-9][A-Z0-9_-]{2,79}$` |  |
| `content_ref.version` | 是 | `string; pattern=^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$` |  |
| `content_ref.content_hash` | 是 | `string; pattern=^[a-f0-9]{64}$` |  |

### version-set

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/common/version-set.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `api_version` | 是 | `string; minLength=1; maxLength=64` |  |
| `event_version` | 是 | `string; minLength=1; maxLength=64` |  |
| `policy_version` | 是 | `string; minLength=1; maxLength=96` |  |
| `world_rules_version` | 是 | `string; minLength=1; maxLength=96` |  |
| `teaching_spec_version` | 是 | `string; minLength=1; maxLength=96` |  |
| `skill_version` | 否 | `string; minLength=1; maxLength=96` |  |
| `artifact_sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `compiler_version` | 否 | `string; minLength=1; maxLength=96` |  |
| `sandbox_image_digest` | 否 | `string; minLength=1; maxLength=256` |  |
| `test_suite_version` | 否 | `string; minLength=1; maxLength=96` |  |
| `prompt_version` | 否 | `string; minLength=1; maxLength=96` |  |
| `model_version` | 否 | `string; minLength=1; maxLength=128` |  |

### error

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/common/error.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `code` | 是 | `枚举 INVALID_REQUEST, SCHEMA_VERSION_UNSUPPORTED, CONTENT_VERSION_MISMATCH, AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, POLICY_DENIED, NOT_FOUND, PAYLOAD_TOO_LARGE, IDEMPOTENCY_KEY_REUSED, WORLD_REVISION_CONFLICT, EVENT_SEQUENCE_GAP, SKILL_NOT_CERTIFIED, SKILL_VERSION_MISMATCH, ACTIVE_SKILL_ARTIFACT_MISMATCH, SANDBOX_COMPILE_ERROR, SANDBOX_RUNTIME_ERROR, SANDBOX_RESOURCE_LIMIT, WORLD_RULE_REJECTED, DEPENDENCY_UNAVAILABLE, FEISHU_SIGNATURE_INVALID, FEISHU_REPLAY_DETECTED, FEISHU_SYNC_FAILED, RATE_LIMITED, UNKNOWN_COMMIT_STATE, INVARIANT_VIOLATION, INTERNAL_ERROR` |  |
| `category` | 是 | `枚举 VALIDATION, AUTHENTICATION, AUTHORIZATION, POLICY, CONCURRENCY, SKILL, SANDBOX, WORLD_RULE, DEPENDENCY, INVARIANT, RATE_LIMIT, INTERNAL` |  |
| `retryable` | 是 | `boolean` |  |
| `user_message_key` | 是 | `string; pattern=^[a-z][a-z0-9_.-]{2,127}$` |  |
| `stage` | 是 | `string; pattern=^[A-Z][A-Z0-9_]{2,63}$` |  |
| `message` | 否 | `string; minLength=1; maxLength=512` |  |
| `details` | 否 | `object` |  |
| `evidence_ids` | 否 | `array; maxItems=64` |  |

### evidence-ref

完整字段、条件必填及嵌套约束：[Schema](../../../agent/contracts/schemas/common/evidence-ref.schema.json)。表格中的子字段必填指父对象存在且选择该分支时。

| 字段 | 必填 | 类型／约束 | 说明 |
| --- | --- | --- | --- |
| `evidence_id` | 是 | `string; pattern=^evidence_[A-Za-z0-9_-]{8,128}$` |  |
| `evidence_type` | 是 | `枚举 DOMAIN_EVENT, ACTION_LOG, SANDBOX_LOG, TEST_REPORT, POLICY_DECISION, WORLD_COMMIT, LEARNER_UPDATE, AUDIT_LOG` |  |
| `created_at` | 是 | `string; format=date-time` |  |
| `sha256` | 否 | `string; pattern=^[a-f0-9]{64}$` |  |
| `uri` | 否 | `string; minLength=1; maxLength=1024` |  |
