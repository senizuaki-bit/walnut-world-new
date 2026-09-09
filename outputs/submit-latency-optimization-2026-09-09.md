# 正确代码提交耗时优化与实测

2026-09-09；同一份实际存档的独立 PostgreSQL 副本、真实 DS、Docker GCC 和 Godot 正式 Run 按钮。原存档未写入本次 Run。

## 实测结果

所有用户等待时间均从点击运行按钮起算，进入游戏前的初始化不计入。

| 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 界面返回运行结果 | 243.989 秒，最终误报失败 | 45.165 秒，成功 |
| 收到成长总结 | 未生成 | 66.559 秒，真实 provider、非降级 |
| Turn 后端任务结局 | DEAD_LETTER | SUCCEEDED |
| Turn 接收到全部收尾完成 | 223.714 秒 | 32.365 秒 |
| Book 上下文每次 SQL | 6,358 | 479 |
| Book 上下文耗时 | 5 次失败，合计约 82.14 秒 | 2 次成功，1.343 + 0.938 秒 |
| Turn.execute 全部尝试 SQL 合计 | 41,826 | 8,816 |
| 确定性错误的无效退避等待 | 约 115 秒 | 0 秒 |

点击后等待运行结果减少约 81.5%。这是一次同存档实测，不是多轮平均值；Docker 和数据库运行速度有波动。

本次各用户可见步骤：

| 步骤 | 本步骤耗时 | 点击后累计 |
| --- | ---: | ---: |
| Build、认证及前端读取结果 | 19.788 秒 | 19.788 秒 |
| 激活及读取结果 | 3.623 秒 | 23.411 秒 |
| 执行、世界提交、Evidence 和 Snapshot 确认 | 21.754 秒 | 45.165 秒 |
| 后台总结、学习记录及最终反馈读取 | 21.394 秒 | 66.559 秒 |

以上阶段含轮询和读取时间。内部阶段存在嵌套，不应再与此表相加。Build 内 Docker 编译运行约 12.594 秒；真实 DS 三次网络调用为 0.969、1.312、2.359 秒，每个 dispatch 只生成一次。Turn 的 4 次执行含 3 次正常异步模型等待恢复，不是 4 次失败重试。

## 代码修改

1. `workflow_worker.py` 识别被 `AgentRuntimeAuthorityError` 包装的确定性 `WorkflowInvariantError`，立即终止该错误的无效重试。临时网络、数据库和边界错误沿用原重试机制。
2. Book 总结使用当前成功 Run、Skill 版本记录和已有学习档案，不再读取整个 Session 的历史 Run。新上下文不提供 `get_session_runs`，公开反馈不再声称全历史尝试/失败次数；保留旧完整上下文和历史工具回执的兼容验证。
3. 前端使用已有 `Command.links.run` 和 `GET /v1/runs/{run_id}`。只有真实 Run 成功且世界提交完成，并通过原 Evidence、事件、receipt、Snapshot 验证后才提前展示成功。成长总结使用现有 `pending_operations` 保存身份并在后台收取，重启后可恢复；后台失败或旧总结晚到不会覆盖新的运行结果。

没有新增后端接口、服务或队列。总结来源仍按现有 Run feedback 和 Interaction 精确匹配。

## 原因与边界

旧历史 Run `run_7b12401f6e9dd90128773374` 保存的 `failure_count=1`，当前重算得到 `2`。以前每次正确代码完成后，Book 都重新扫描并校验它，再为这个不会自行恢复的错误反复重试。本次改变当前完成总结需要的数据范围，没有改写旧存档的历史证据。

另外，空客户端缓存首次加载完整旧聊天记录仍较慢：两次启动在历史列表请求超时，尚未点击提交，不纳入上表。最终有效计时仅在测试脚本的启动阶段将 HTTP 等待放宽到 60 秒，点击前恢复原超时及轮询设置；正式前端超时未修改。有效样本的启动耗时为 22.445 秒，另有一次计时脚本初始化错误的无效启动，也未纳入提交结果。首次历史加载是仍待单独处理的性能点。

## 验证

- Agent runtime：144 项通过；自然文案微调后重跑相关 3 项通过。
- 后端：26 项相关单元/集成测试通过；最终 Book 修改后额外重跑 4 项成功、学习记录、Provider 恢复场景，全部通过。
- 前端：10 个聚焦 Godot 测试脚本通过，包括提前成功、拒绝未提交结果、错误 receipt、总结失败、磁盘恢复、去重和下一轮隔离。
- Ruff 与 `git diff --check` 通过。
- 实际 Run：`run_8a9eb1be5b506cf5e107f5b4`，`SUCCEEDED / COMMITTED`；世界 revision 11 → 12，仅一次提交。
- 实际成长总结：`interaction_4839ddc5a62a20f7025cdbb1`，`book_agent / growth_summary`，`provider / degraded=false`。
- Learner projection：`SUCCEEDED`、attempt 1；客户端待处理操作数为 0，Interaction 游标 104。

## 日志

- [优化前诊断](submit-latency-diagnosis-2026-09-09.md)
- [优化后汇总](submit-latency-optimized-summary.json)
- [界面时间线](submit-latency-optimized-ui.jsonl)
- [Worker 内部阶段](submit-latency-optimized-worker.stdout.log)
- [真实模型调用](submit-latency-optimized-relay.stdout.log)
- [HTTP 及 SQL](submit-latency-optimized-gateway.stdout.log)
- [最终写入与客户端状态验证](submit-latency-optimized-validation.jsonl)
- [旧历史差异](run-outcome-difference.log)

日志不包含认证密钥。测试只写独立副本；结束后移除本次测试数据库并恢复测试前的容器状态。
