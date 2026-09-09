# 后端四项问题修复记录

范围：Demo 后端。沿用现有 PostgreSQL 和 worker 进程，不增加外部服务；本轮未修改前端产品代码。

## 1. 并发保存时 workspace 偶发 500

原因是一次读取中的 workspace、session、draft 和 world 来自不同时间点。保存草稿恰好插在中间时，一致性校验将正常更新误判为数据错误。

修复：workspace 读取放进 PostgreSQL `REPEATABLE READ READ ONLY` 事务。一次请求读同一份快照，下一次请求读取最新状态。保留原有一致性校验。

验证：在读取 workspace 行后，用另一个数据库会话真实保存草稿。保存、并发读取、随后读取均成功，随后读取的 workspace_revision 增大。原先复现返回 INVARIANT_VIOLATION，现在正常返回。

## 2. DS 文字聊天没有历史

原因是 `PostgresAgentRuntimeReads.list_recent()` 固定返回空元组。

修复：按当前租户、学生、session、内容版本定位当前 Turn，查询它之前最近最多 8 条有效问答，按时间正序传入 Agent 提示词。每条同时包含学生提问和 Agent 回答；排除当前轮、其他用户与其他 session。Hint 已冻结的上下文仍用于 provider 重试。

验证：实际两轮 Hint 执行中，第二轮 provider 请求包含第一轮问题；当前问题不出现在历史里，其他用户和 session 返回空。已有 103 条 interaction 的数据副本能返回最近 8 条，修复前为 0。

## 3. 同一次读取反复查询相同数据

修复：历史列表、单条 interaction、Run 和 Evidence 的读取，在各自只读快照内复用相同 SELECT 和参数的结果。原校验流程继续执行。缓存仅属于当前请求，写入路径不使用；新请求不会继承旧结果。

相同数据副本、相同读取参数的直接适配器测量：

| 读取 | 修复前 SQL | 修复后 SQL | 修复前耗时 | 修复后耗时 |
| --- | ---: | ---: | ---: | ---: |
| 旧历史页，after=50 / limit=50 | 4,624 | 751 | 14.522 秒 | 3.999 秒 |
| 已完成且有反馈的 Run | 1,698 | 172 | 5.987 秒 | 2.256 秒 |

这是单次本地对照测量，不是并发压测或耗时承诺。剩余查询仍用于读取不同的关联事实。

验证包括：不同 SQL 参数不混用；同一请求仍看到一致快照；另一个事务写入后，新请求能看到更新。

## 4. 问叮当被 Build / Book 阻塞

修复：同一 worker 进程运行两条有界执行循环。一条只处理无 skill binding 的 MESSAGE（Hint），另一条处理 Build、正式运行和 Book 等任务。保留原来的任务认领、租约、重试及 fencing。绑定技能的 MESSAGE 仍走正式运行通道。

验证：真实 PostgreSQL 中，后台 handler 暂停时，另一个 worker 能立即认领并完成 Hint；带技能绑定的任务不会被 Hint 通道拿走。

实际端到端运行额外发现：learner 交接提前冻结消息序号，Hint 抢先发布后会占用这个序号；workspace 时间也可能已被 Hint 推进。已改为在持有 session 锁的发布事务中分配消息序号，交接序号作为下界，最终序号由提交回执记录。workspace 发布时间使用当前时间下界，保留已冻结的 learner 事件时间。现有连续序号、回执和数据一致性校验继续执行。

新增 `hint_before_learner` 回归：完成 Book 交接 → 完成 Hint → 执行 learner 发布 → 验证列表、Run、Command、workspace 均可读取。修复前稳定失败于 `Interaction sequence differs from the frozen gap-free hand-off`，修复后通过。

## 验证材料

自动化检查：第一批后端 unit 与相关 PostgreSQL 集成回归 421 项通过（225.89 秒）。追加的 learner / Hint / 语音 / 四项修复检查中，33 项直接通过；1 项旧 provider recovery 测试因入队与认领发生在同一时刻、主机与数据库短暂时钟差而失败。将测试改为现有 worker 的有界轮询方式后，该项与新并行竞态用例复验均通过（2 passed，22.06 秒）。两批测试有重叠，不相加为独立用例总数。修改文件 Ruff 检查通过。

真实 Godot → 后端 → Docker 沙箱 → DS 流程（`backend-fixed-final-*`）：

| 观察点 | 上一次实测 | 本次实测 |
| --- | ---: | ---: |
| 启动恢复完成 | 27.244 秒 | 17.012 秒 |
| 点击运行 → 显示运行成功 | 47.311 秒 | 41.377 秒 |
| 成功后立即提问 → 收到回答 | 29.299 秒 | 17.382 秒 |
| 提问获 202 → worker 开始处理 | 约 10.7 秒 | 约 0.075 秒 |
| 点击运行 → 总结恢复完成 | 81.745 秒 | 70.727 秒 |

排队时间按客户端收到 202 与 worker 开始执行的 UTC 日志计算；数据库 created_at 受因果时间下界影响，本次不能直接当作墙钟起点。上述端到端时间包含模型和本机运行波动。

本次 Run 为 `run_ae410e2f0d4d03c36a01d5d7`，状态 SUCCEEDED，world_application 为 COMMITTED。Hint 的反馈来源为 provider，degraded=false，引用这个最新 Run；learner 投影成功且仅执行一次；四个 workflow job 全部 SUCCEEDED；前端最终 summary 为 READY、pending_count=0。原数据库当天新增 Run 为 0。

保留一个已有前端现象：紧接运行提交 Hint 时，第一次请求因 Turn 序号落后返回 400；前端刷新后自动重试并成功。本次后端修改和耗时记录均包含这次恢复，没有修改前端。

- `backend-remaining-reproduction.json` / `backend-remaining-fixed.json`：修复前后复现。
- `backend-read-cost-before.json` / `backend-read-cost-after.json`：同一数据副本读取成本。
- `walnut-world-backend/tests/integration/test_backend_response_fixes.py`：四项新增回归测试。
- `backend-fixed-final-summary.json` / `backend-fixed-final-validation.json`：真实完整流程的耗时与状态检查。
- `backend-fixed-comparison.json`：从两次原始日志重新计算的端到端对照。
- `backend-fixed-*`（不含 final）保留第一次并行实测暴露的 learner 序号竞态日志；`backend-fixed-final-*` 为修复后的成功重跑。

测试在独立数据库及原数据副本中运行，未向日常数据库写入测试任务。测试结束后停止临时服务、删除三个测试数据库，PostgreSQL 容器恢复为本轮开始时的停止状态。
