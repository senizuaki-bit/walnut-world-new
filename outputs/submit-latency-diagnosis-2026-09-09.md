# 提交代码后等待数分钟：真实运行诊断

2026-09-09，使用用户当前持久化存档的独立数据库副本、原游戏 Godot 4.5.2、正式 AppRoot / RunButton 和真实 DeepSeek V4 Flash，提交一次正确的浇水代码。保留正式场景默认开关和 360 秒轮询期限，未修改业务源代码或原游戏存档。

## 结论

从点击运行到前端显示失败共 **243.989 秒（4 分 4 秒）**。代码编译、执行已成功；后端在为 `book_agent` 准备成长总结上下文时，读取历史 Run 的校验失败。该确定性异常被当作可重试错误，反复校验同一历史并等待，最终才返回 `TURN_COMMAND_FAILED`。

## 本次时间线

前端累计时间以点击正式运行按钮为零点：

| 阶段 | 累计时间 | 结果 |
|---|---:|---|
| 点击运行 | 0 秒 | 仅提交一次，497 字节正常浇水代码 |
| 编译 / 测试结果返回 | 13.978 秒 | CERTIFIED |
| 激活结果返回 | 16.805 秒 | 成功 |
| Run 结果形成 | 约 33 秒 | 后端产生 task_completed 事件 |
| 最终提交结果返回 | 243.989 秒 | TURN_COMMAND_FAILED |

后端 Turn 从接受到最终失败为 **223.714 秒**。两次真实 Provider 请求均成功、各只生成一次，数据库记录耗时分别 **0.655 秒、0.819 秒**，合计约 **1.47 秒**。未进入最终成长总结的模型生成阶段。

提交后的 73 次 HTTP 请求均完成，前端逐帧观测最大耗时约 **1.046 秒**；本次没有出现 15 秒请求超时。启动恢复存档约 14 秒，不计入上述点击后耗时。

## 时间消耗在哪里

错误链为：

```text
ContextBuilder.build(book_agent)
  → list_session_runs
  → validate_terminal_projection
  → validate_canonical_outcome_event
  → WorkflowInvariantError: Run outcome event differs from its canonical suffix
  → AgentRuntimeAuthorityError
  → 普通失败重试
```

| Attempt | 结果 | 本次执行 SQL | 失败后等待 |
|---|---|---:|---:|
| 1 | Provider 协调等待 | 1,485 | 约 1 秒 |
| 2 | Provider 协调等待 | 2,576 | 约 1 秒 |
| 3 | 历史校验失败 | 7,553 | 8 秒 |
| 4 | 同一历史校验失败 | 7,553 | 16 秒 |
| 5 | 同一历史校验失败 | 7,553 | 32 秒 |
| 6 | 同一历史校验失败 | 7,553 | 60 秒 |
| 7 | 同一历史校验失败，最终终止 | 7,553 | 无 |

每次失败中的 `ContextBuilder.build(book_agent)` 都执行 **6,358 条 SQL**；5 次累计 **31,790 条 SQL、82.140 秒**。Turn 的 7 次执行总计 **41,826 条 SQL**，计数不含领取任务、失败记录等包装逻辑。四次错误后的纯退避等待累计 **116 秒**。阶段计时包含子阶段，不能重复相加。

## 代码原因

- `walnut-world-backend/src/walnut_backend/adapters/postgres/agent_runtime.py:99`：`AgentRuntimeAuthorityError` 继承普通 `RuntimeError`。
- 同文件 `:739–746`：历史 Run 校验抛出的 `WorkflowInvariantError` 被包装为 `AgentRuntimeAuthorityError`。
- `walnut-world-backend/src/walnut_backend/workers/workflow_worker.py:353–369`：失败分类只检查最外层异常，因而没有命中“历史一致性错误立即结束”的分支。
- 同文件 `:340–350`：按照领取次数指数退避，上限 60 秒；Provider 的正常协调等待也计入领取次数，所以第一个真正失败出现在 attempt 3。
- `walnut-world-frontend/scripts/client/command_poller.gd:24`：前端最多等待 360 秒，并非固定等待六分钟。本次是在后端最终失败后正常显示错误。

此前存档中同类任务 `job_43a439ec7f3511a6d101b64b` 已耗时 **223.259 秒 / 7 次领取**，对应两次 DS 请求分别 **1.140 秒、1.061 秒**。本次复现了同样的错误和重试路径。

历史事件为何与当前计算结果不同，还需要对出错的具体历史 Run 逐字段比较。报错比较的是整个事件 JSON，现阶段不能仅凭文案断言是失败次数、时间戳或其他字段造成。后续修复应同时处理这处历史不一致，以及异常包装造成的无效重试。此前聊天列表的请求内去重修复不能消除这两项问题。

## 证据与复跑工具

- `outputs/submit-latency-ui.jsonl`：正式前端逐阶段事件和请求耗时。
- `outputs/submit-latency-worker.stdout.log`：后端阶段计时与 SQL 次数。
- `outputs/submit-latency-worker.stderr.log`：完整异常链和重试记录。
- `outputs/submit-latency-relay.stdout.log`：真实 Provider 时间与 token 使用量，不含正文或密钥。
- `outputs/submit-latency-summary.json`：数据库任务、步骤凭据时间和 Provider 摘要。
- `outputs/submit-latency-history.log`：原存档中的同类慢任务证据。
- `outputs/submit_latency_probe.gd`、`submit_latency_runtime.py`、`run_submit_latency_probe.py`、`start_submit_latency_probe.ps1`、`collect_submit_latency.py`：隔离诊断工具，未进入业务运行入口。

准备阶段的功能开关不匹配启动记录，以及计时脚本序列化失败记录均已单独保存，未计入本次指标。有效运行重新从原存档复制数据库后开始。临时服务及数据库副本在采集完成后清理，原数据库恢复测试前的停止状态。此次工作只定位并记录问题，尚未修复业务代码。
