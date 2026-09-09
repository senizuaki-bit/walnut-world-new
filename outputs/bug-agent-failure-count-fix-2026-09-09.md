# Bug Agent 错误计数修复与验证

## 修复结果

同一会话、同一世界、同一技能的连续相同运行错误，现在跨代码版本累计。第三次失败触发 Bug Agent。成功运行、不同错误类型、不同技能或世界仍会终止这段连续计数。

原先比较整个 `SkillRef`，其中包含版本、认证与产物标识。学生修改代码重新构建后，这些标识改变，计数被重置为 1。后端计数、Bug 历史查询、Agent 上下文校验三处已统一到同一技能的连续失败记录；当前运行与证据的版本校验仍保留。

新失败结果内部记录 `failure_count_scope: skill`，旧结果仍按其原有版本规则验证，重试沿用已保存的结果，不需要数据库迁移。

修改文件：

- `walnut-world-backend/src/walnut_backend/adapters/postgres/run_outcomes.py`
- `walnut-world-backend/src/walnut_backend/adapters/postgres/agent_runtime.py`
- `agent/python/yaya_agent_runtime/context_builder.py`
- 对应后端计数测试与 Agent 上下文测试。

## 实际链路验证

在独立数据库副本中，通过 Godot 实际提交处理函数调用后端，使用真实 DS 与 Docker 执行。三次提交源代码摘要不同，生成三个不同技能版本和认证，保持同一 `task_incomplete` 错误。代码差异含注释，因此编译产物摘要相同；此次验证针对重新构建后版本变化导致的计数重置。

| 轮次 | Run | 错误计数 | 回复角色 | 后台状态 |
| --- | --- | --- | --- | --- |
| 1 | run_45584a290b3b98989236863b | 1 | teaching_agent | SUCCEEDED |
| 2 | run_e3c1565ff5ac0709d607ee00 | 2 | teaching_agent | SUCCEEDED |
| 3 | run_3570fc9c264f6f37822b01de | 3 | bug_agent | SUCCEEDED |

三轮均为 `source=provider, degraded=false`，学习记录更新任务也全部成功。测试游戏进程达到总计 450 秒的脚本时限后退出；第三轮随后在后台完成。重新启动 Godot 并恢复同一会话，成功读取到这条 Bug Agent 回复。没有将首次脚本超时记为无中断端到端通过。

实测耗时仍偏长：前两轮从提交到游戏收到回复约 96、124 秒；第三轮从提交到回复记录保存约 219 秒，到后台成功约 231 秒。Bug 上下文构建被多次重复执行，每次约 13–21 秒、5,663 条 SQL。这次解决的是计数与触发问题，不能据此宣称提交响应速度已解决。

## 自动化验证

- 后端单元测试、Hint 集成测试、同会话 Teaching→Teaching→Bug→Book 流程测试：405 passed，1 条已有依赖警告。
- 最终计数边界、Agent 上下文和路由定向回归：46 passed，5 subtests passed。与上一组有重叠，不相加作为独立测试总数。
- Ruff 检查通过。
- 旧复现脚本现在返回：同版本三次失败为 3，重新构建版本三次失败也为 3。

验证数据见 `bug-versions-live-final-validation.json`；游戏恢复结果见 `bug-versions-recovery-godot.log`；后台日志保存在同目录 `bug-versions-live-final-*.log`。原数据库中没有这三条测试运行记录。临时数据库与测试服务在验证后清理。
