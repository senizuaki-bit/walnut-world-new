# 后端剩余问题检查

范围：当前工作树的 Gateway、PostgreSQL 读写、任务调度、DS 文字上下文与豆包上下文入口。按 demo 的实际体验排序；本轮没有修改业务代码。

## 1. P1：workspace 读取仍存在并发误报

位置：`walnut-world-backend/src/walnut_backend/adapters/postgres/product_workspaces.py:28`。

`get()` 分别读取 workspace、session、world snapshot、drafts 和交互游标，采用默认 READ COMMITTED，最后要求这些数据严格一致。草稿保存会在同一事务更新 draft 和 workspace，但读请求可先取到旧 workspace，再取到新 draft，于是把正常提交误判为数据损坏。

本轮在原存档的独立 PostgreSQL 副本，调用实际 `PostgresProductDraftStore.upsert()` 保存草稿，并在 `PostgresProductWorkspaceStore.get()` 读出 workspace 后插入该保存操作：

- 保存前读正常；保存成功；并发读取返回 `INVARIANT_VIOLATION`；保存后再次读正常。
- 仅在测试会话改用 `REPEATABLE READ READ ONLY`，同样时序正常。
- 公共 workspace 路由会把该错误按错误目录映射为 HTTP 500。复现直接调用实际 adapter，没有另跑 HTTP 网络请求。

影响：保存/刷新重叠时可出现偶发页面错误。豆包 `load()` 同样先读 workspace，并将读取失败转换成 `VOICE_SESSION_UNAVAILABLE`，因此此竞态也可能影响语音连接或上下文刷新；本轮未额外做真实语音断线复现。

建议：沿用已经用于 Run/Evidence/Interaction 的一致性只读事务。测试已表明该方案能消除本次时序下的误报，不需要新服务或锁住写请求。

## 2. P1：DS 文字提问未读取历史对话

位置：`walnut-world-backend/src/walnut_backend/adapters/postgres/agent_runtime.py:789`。

`PostgresAgentRuntimeReads.list_recent()` 丢弃参数后直接返回空 tuple。Worker 确实把该 adapter 作为 `ContextBuilder.messages`；teaching_agent 请求最近 8 条消息，PromptBuilder 只有在结果非空时才加入 recent_messages。

本轮实读副本：当前会话 113 个 Turn、103 条 Interaction，但 `list_recent(session_id, 8, context)` 返回 0 条。这是实现缺失，不是模型响应慢造成的。

影响：“继续刚才那一点”“你上次说的第二种方法”等依赖前文的文字提问，缺少此前对话依据，容易答非所问或重复提示。当前任务、最新 Run 和 learner 信息仍存在，因此不能表述为整个上下文为空。豆包同一实时连接保留上游对话，不属于此问题。

建议：按会话读取有界的最近几轮学生消息与 Agent 回答，排除当前及之后的 Turn；复用本次 Hint 已有的上下文快照，避免等待 Provider 时历史变化造成请求 hash 漂移。

## 3. P2：读取链仍执行大量重复关联校验

位置：`run_evidence.py:320`、`product_interactions.py:234`，以及它们调用的 Run/terminal projection 校验。

下列数字来自上轮最终真实联调的原始 HTTP/SQL 日志，本轮重新解析核对，未把它们称为新一轮完整性能测试：

| 读取 | SQL | 耗时 |
| --- | ---: | ---: |
| 启动时一页旧交互历史 | 4,624 | 16.734 秒 |
| 已完成 Run | 1,821 | 7.031 秒 |
| 运行后两条新 Interaction | 903 | 3.750 秒 |
| Run Evidence | 448 | 1.375 秒 |
| World Evidence | 448 | 1.546 秒 |

前一轮已修复的“失败后 5 轮聊天递归膨胀”仍有回归保护，但不能把该小样本的 610 SQL 当成所有真实存档的上限。读 Run 会验证 Command、加载 Run 关联事实并验证 terminal projection；不同 HTTP 请求不会共享前一个请求的校验结果。

影响：启动加载旧记录、显示结果和收取总结仍明显慢。建议优先减少历史读取的依赖范围、批量加载重复关联、复用同一请求内验证结果；保留 actor/session/版本/receipt 等必要一致性检查。

## 4. P2：互动提问仍被慢工作流排队阻塞

位置：`workers/workflow_worker.py:170/185`、`adapters/postgres/workflow_jobs.py:257`、`worker_main.py` 的单 worker 装配。

当前 worker 逐个 await handler，Build、运行、Book 总结和 DS Hint 共用该循环；领取任务按 created_at 排序。后台总结不再阻塞“成功”显示，但这不等于已获得独立执行资源。

最终真实日志中，Hint POST 202 返回于 UTC 06:52:47.493，Hint handler 首次开始于 06:52:58.180，约 10.7 秒排队；其前面的 Book 执行刚好在 06:52:58.155 结束。整次 DS 文字回答约 29.3 秒，模型生成约 2.12 秒。另有 Hint `_prepare` 两次各约 3 秒、890 条 SQL；固定上下文已经省去第二次 ContextBuilder 构建，但没有消除这一部分工作。

建议：优先避免后台总结长期占用唯一互动执行槽，再处理重复 prepare。单纯更换模型或缩短前端轮询，无法消除这些服务端等待。豆包实时 WebSocket 不经过该持久工作流队列。

## 验证与边界

- 本轮执行：`cd walnut-world-backend; .venv/Scripts/python.exe -m pytest tests/unit -q --disable-warnings --maxfail=5`，结果 **394 passed, 1 warning in 24.00s**。
- 本轮复现命令：`.\walnut-world-backend\.venv\Scripts\python.exe .\outputs\backend_remaining_probe.py`，约 2.6 秒完成；输出见下方 JSON。脚本针对已有本地存档配置和本次专用副本，不会自行创建数据库。
- 本轮新增验证只涉及数据库副本，没有调用真实模型或修改原存档；没有宣称跑完全部 PostgreSQL 集成/真实语音测试。
- 业务代码保持不变。临时库 `walnut_backend_review_20260909` 已删除，PostgreSQL 容器恢复检查前的停止状态。
- 建议顺序：先修 workspace 并发误报，再补文字历史，然后集中处理重读与排队。

## 证据

- [本轮复现结果](backend-remaining-reproduction.json)
- [本轮复现脚本](backend_remaining_probe.py)
- [真实 HTTP 与 SQL 日志](dingdang-freshness-gateway.stdout.log)
- [真实 Worker 日志](dingdang-freshness-worker.stdout.log)
- [真实界面时间线](dingdang-freshness-ui.jsonl)
- [上一轮状态与性能验证](dingdang-freshness-optimization-2026-09-09.md)
