# 提交与 Bug Agent 链路耗时检查

本次只分析已有真实运行记录与当前代码，未修改业务实现、未重新调用收费模型。样本为 2026-09-09 第三次跨版本失败，任务 `job_e6339a45216d12135cbe1f10`、运行 `run_3570fc9c264f6f37822b01de`。

## 总体结果

主要瓶颈在后台重复读取、验证和恢复流程。五次真实 DS HTTP 请求合计 6.25 秒；Turn Worker 执行六轮，累计 182.234 秒、59,627 条 SQL。不能把六轮称为六次真实报错：前五轮都是模型资源尚未完成时的 `DurableLlmDispatchPending`。

| 不重叠的时间区间 | 秒 | 说明 |
| --- | ---: | --- |
| 点击提交 → Turn Worker 开始 | 32.40 | 保存、构建、激活、受理与客户端等待；构建 Worker 为其中的 16.16 秒 |
| Turn Worker 六轮实际处理 | 182.23 | 含上下文、执行、结果计数/验证、模型状态读写和最终结果处理 |
| 六轮之间的时间间隔 | 4.63 | 调度、等待与恢复；与模型耗时存在重叠 |
| Turn Worker 结束 → 后台任务成功 | 12.08 | 学习记录交接、投影和最终状态完成的时间窗口 |
| 总计 | 231.35 | 提交至后台全部完成 |

回复记录在提交后约 219 秒写入，当时事务/最终状态尚未全部完成。首次 Godot 脚本达到了整个测试的 450 秒时限，第三条回复是在恢复会话后验证的；这里不是连续客户端接收耗时。模型 6.25 秒不另加到表中，模型与 Worker 跨进程并行。

## 六轮 Worker 内部

| 阶段 | 次数 | 累计秒 | SQL |
| --- | ---: | ---: | ---: |
| `_prepare` | 6 | 7.265 | 2,394 |
| 小核桃上下文 | 2 | 6.453 | 2,390 |
| 已执行 Skill 的恢复上下文 | 4 | 4.141 | 1,612 |
| Bug 上下文 | 4 | 65.517 | 22,652 |
| 持久化模型适配器调用 | 12 | 1.373 | 124 |
| `_finish` | 1 | 2.625 | 994 |
| 未单独打点的其余处理 | — | 94.860 | 29,461 |

以上为互不包含的子阶段与余量。最后一行包括运行调用、结果推导与验证、最终回复验证、工具/trace 等，现有日志不足以进一步精确划分；不能把 94.86 秒全部叫作 SQL 执行或全部归给某一个函数。模型适配器的 12 次调用含结果重放，不是 12 次真实模型生成。

## 已确认原因及优化顺序

### 1. 优先复用提交结果的 Agent 上下文

`PostgresDurableLlm.generate` 遇到 PENDING 抛出异常；Worker 下次重新调用 `TurnWorkflowHandler.execute`。虽然 `SKILL_INVOKED` 和模型结果已有持久化记录，`execute` 仍重新准备、恢复 Skill、执行 `outcomes.derive`，然后重新 `ContextBuilder.build`。

本次 Bug 上下文构建四次，每次 5,663 条 SQL，第一次约 21.1 秒，后面三次合计 44.4 秒。可以像当前 `_hint_context` 使用 `HINT_CONTEXT_READY` 一样，为提交后的 FINAL 上下文保存任务内快照，模型等待后直接复用。首次完整准备、后续恢复读取快照，不需要新缓存服务或改前端接口。

这是最直接的优化：日志证明存在三次重复构建、约 44.4 秒的工作量；净收益还需扣除快照读取成本并重新实测。

### 2. 同一阶段共享读取快照，停止反复重验历史的整条构建链

`get_run`、`list_same_failure_runs` 等读取各自开启数据库 Session；`load_validated_run` 未传 `validation_state` 时直接进入无缓存路径。`derive`、最终决定保存、学习投影又各自重新开始校验。

历史 Run 校验还会进入 `validate_run_provenance → _validate_run_activation / validate_build_provenance`，继续查询 Build、认证、命令、任务回执、草稿等。请求内的 `TerminalProjectionValidationState` 能避免一部分递归重复，但无法跨这些独立调用复用。之前的 `snapshot_read` 查询复用主要用于接口读取，当前 Agent 读取仍直接使用 `self._sessions()`。

建议先让一次只读上下文组装共享一个数据库快照与校验状态，并批量读所需 Run、Evidence、Build 记录。写入与世界变化仍在其事务边界验证，避免缓存可变状态。这比加索引或增大连接池更贴近已经观察到的问题；本次没有查询计划证据支持先做索引改造。

### 3. 已保存的结果按阶段恢复，错误计数逐次累计

即使存在 `OUTCOME_DERIVED`，当前 `derive` 仍调用 `validate_canonical_outcome_event`，再计算失败后缀；最终决定保存、学习投影又验证相关结果。

可以在新运行完成时计算并保存一次结果、失败计数和用于解释的失败记录。相同会话/世界/技能/错误则接续上一次计数，成功或错误类型变化则重置；跨版本保留本次修复的语义。恢复时校验任务与已保存结果标识，不再遍历所有来源事实。旧结果的计数规则继续兼容。

若限制 Bug 提示只读最近三到五次细节，必须同步调整当前“历史长度等于 failure_count”的内部约束；不能只给 SQL 加 LIMIT 就称为优化。

### 4. 模型短等待留在当前阶段，避免每次立即退回整个任务

本次模型请求分别为 0.750、1.422、0.984、0.890、2.204 秒。第三个请求在 07:49:15.162 已完成，Worker 在 07:49:48.944 才读回结果；中间主要在重新准备上下文和验证。

可在现有持久化 dispatch 上做数秒的异步等待，快速完成就接着执行；确实长等待或进程重启才走恢复。不要去掉已有 dispatch 标识和沙箱执行回执，否则可能重复运行。这个优化与第 1 项收益重叠，不相加估算。

### 5. 回复展示与学习画像更新分开完成

当前学习投影会再次加载 Run、验证目标，然后写学习记录、Interaction 和最终状态。Turn Worker 到后台成功间隔约 12.1 秒。即使队列已拆开，失败回复仍要等这段链路完成。

可以在运行结果、Bug 回复确定后先提供可读取的反馈状态，学习画像异步更新。下一轮仍能读取最新运行与错误计数，不能将最新运行状态也一起延后。后端可以先保留接口；即时展示的最终收益需要客户端使用该状态。

### 6. 构建和前端状态同步属于次优先级

此次构建为 16.16 秒，其中构建调用 15.16 秒；这是实际工作，不能删掉。后续可测 Docker 启动/复制/编译各段，对完全相同源码与编译配置复用产物。此次源码摘要不同，即使只是注释差异，也不能直接声称相同源码缓存会命中。

第三轮提交 Turn 时先返回 400（2.609 秒），随后刷新 workspace/world 并再次提交成功。第二轮也出现同样现象。前端存在拒绝后刷新 Turn 序号/世界游标再重试的分支，与日志路径相符；日志未保存 400 的响应体，不能断言具体字段。应在当前完成/激活结果中及时同步游标，消除可避免的首次拒绝。

命令轮询共 58 次、HTTP 处理累计 3.17 秒，单次最长 0.641 秒；大部分发生在后台工作的同时。轮询最大间隔 4 秒会延迟发现已完成结果，但不是这次三四分钟的主因。

## Demo 的实施建议

先完成 1、2、3：复用最终上下文、合并只读历史查询、恢复时直接使用已完成阶段。随后做短模型等待和学习画像异步更新。保留真实编译、真实执行、必要的当前状态检查；不增加新的缓存集群、队列系统或生产级设施。

最先验收的指标是：一次 Bug 请求只完整构建一次上下文；等待模型时不重复扫描历史；第三次失败仍为计数 3 且真实 Bug 回复成功；长会话中最新 Run 可立即读取。具体整体耗时目标必须在改动后重跑同一链路确定，不能据此保证几秒返回。

## 证据与复现

`outputs/analyze_bug_latency.py` 从保存的时间日志生成 `outputs/bug-latency-analysis.json`。执行 `walnut-world-backend/.venv/Scripts/python.exe outputs/analyze_bug_latency.py --check` 会对当前捕获日志报告重复 Bug 上下文：4 次、65.517 秒、22,652 SQL。这是固定日志的分析检查，不是修改后的动态性能回归测试。

关键代码位置：

- `walnut-world-backend/src/walnut_backend/workers/turn_worker.py`：`execute`、`_hint_context`。
- `walnut-world-backend/src/walnut_backend/adapters/postgres/durable_llm.py`：`generate` 的 PENDING 分支。
- `walnut-world-backend/src/walnut_backend/adapters/postgres/run_outcomes.py`：`derive`、`record_final_decision`、`validated_failure_suffix`。
- `walnut-world-backend/src/walnut_backend/adapters/postgres/agent_runtime.py`：`get_run`、`list_same_failure_runs`。
- `walnut-world-backend/src/walnut_backend/adapters/postgres/skill_provenance.py`：运行与构建来源验证。
- `walnut-world-backend/src/walnut_backend/workers/turn_projection.py`：学习投影完成路径。
