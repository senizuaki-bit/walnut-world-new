# 提交后立即问叮当：状态同步与响应优化

日期：2026-09-09。项目按 demo 范围处理，复用现有工作流、回执和接口。

最终真实 Godot + Docker + DeepSeek 提交与文字提问已完整通过；豆包语音上下文自动刷新另用真实连接验证通过。成功运行后可读取最新正式结果，成长总结继续在后台完成。

## 最终真实文字复验

使用原存档的独立 PostgreSQL 副本、正式 Godot 场景、真实 Docker 编译及 DeepSeek。点击前恢复正式 HTTP 超时，保留本轮实际的 Hint 轮询设置。代码运行成功后的下一帧发出“我刚才提交的代码运行成功了吗？请根据最新一次正式运行回答。”

| 阶段 | 本阶段耗时 | 从点击运行累计 |
| --- | ---: | ---: |
| 保存、编译、客户端确认 | 19.517 秒 | 19.517 秒 |
| 激活可运行版本 | 4.205 秒 | 23.722 秒 |
| 正式运行与结果确认 | 23.589 秒 | **47.311 秒** |
| 随后立即文字提问，到回答显示 | **29.299 秒** | 76.623 秒 |
| 后台总结确认 READY、待处理清空 | — | 81.745 秒 |

文字回答明确确认运行成功，反馈引用 `run_20eb707caa3b9fdaa2bed057`，与刚才的正式运行完全一致；`source=provider`、`degraded=false`。提问时客户端仍有对应 `pending_summary`。

本轮所有工作流均成功；唯一 Run 的世界 revision 11 → 12，Learner 投影成功且 attempt 1；最终客户端 pending 为 0、交互游标 105。没有 HTTP 500 或未处理的业务失败。有一次已有的 Turn 游标重同步：首次 POST 400，刷新 workspace/snapshot 后 POST 202 成功；保留在完整验证记录中。

这次重同步按代码和数据库序列证据定位为 `INVALID_REQUEST` / `client_turn_sequence must be the next session sequence`：运行使用序列 114，客户端旧 workspace 仍生成 114，刷新后以 115 提问成功。自动恢复逻辑已存在于 HEAD。本次原始 400 日志未记录响应正文，错误码是证据推断，不能当成直接日志摘录。

真实 Hint 的上下文只构建一次（815 SQL / 2.843 秒），恢复时省去再次构建；两个执行阶段分别为 6.047 秒 / 1731 SQL 与 3.469 秒 / 948 SQL。四次实际模型生成全部成功，均只生成一次，单次模型请求约 1.06～2.31 秒。SQL 的嵌套阶段计数含子阶段，不应相加重复统计。

当前文字入口仍走 DS 的持久工作流，这次 29.3 秒包含排队、读取、模型和客户端确认，并不是豆包语音回答耗时。移除 Book/learner 终态作为 Hint 前置条件，不代表共享 worker 的排队已经消失。豆包实时通道直接读取轻量上下文，实测见下节。

空客户端缓存加载旧历史另耗 27.244 秒，不计入点击后耗时。测试脚本只在启动阶段放宽 HTTP 等待，正式前端超时没有修改。此前有效样本点击至成功为 45.165 秒，本轮 47.311 秒；单次实测存在编译和调度波动，不能据此宣称提交或文字回答总时长必然缩短。

## 已验证的语音同步

真实豆包 WebSocket 保持同一连接，使用后端默认 1 秒检测。客户端只发送合成静音，没有发送 `context`、问题或模型工具请求。

| 事件 | 数据库首次观察到上游更新 ACK |
| --- | ---: |
| 新 Run 已成功、世界已提交、Book 尚未完成 | 0.797 秒 |
| 同一 Run 的 Book 反馈生成，`updated_at` 未变化 | 0.078 秒 |

连接准备耗时 0.937 秒。更新标识包含 Run ID、更新时间和反馈指纹，因此 Book 沿用 Run 时间戳也能触发更新。每秒只有 1 条轻量 SQL；变更后才读取完整上下文。

最终只读基准使用原存档副本：20 次标识查询平均 10.6 ms、最大 27.7 ms；3 次完整上下文平均 42.1 ms、最大 57.9 ms，每次 12 SQL。

计时起点为每 200 ms 查询的数据库首次观察，存在采样误差。`session.updated` 不带 Run ID，日志按当时最新 Run 关联。这验证上游接受上下文，未测麦克风对话、口头复述、生成音频或播放时延。未提交的编辑器内容仍需前端通过 `context` 同步。

## 修改范围

- 文字 Hint 直接引用最近成功且已提交的 Run，移除对 Book 或 learner 终态的前置要求；保留失败历史的既有规则。
- 前端保留成功 Run 的身份，连续提问沿用准确来源；恢复后可采用同次 Hint 响应中已验证的 Run 身份。主动提问可显示普通消息回答。
- 主动文字 Hint 默认轮询上限由 4 秒缩短为 1 秒，尊重服务端 `Retry-After` 和显式设置。Build、Run 和后台总结的默认轮询保持原样。
- Run/Evidence 公开读取使用同一个只读数据库快照，避免提问并发更新 workspace 时混用前后状态。
- 同一次 Hint 首次构建的完整上下文写入现有 JobStepReceipt，Provider 恢复时复用；下一次提问重新读取最新状态。不新增表、服务或通用缓存，不放宽 Provider 不可变输入校验。
- 豆包连接自动刷新当前 Run 与反馈；任务是否完成由正式 Run 和世界提交结果决定。

## 回归发现与修正

第一次真实复验发现 GET Run 在 Hint 并发提交时返回 500：一次请求混读了不同版本的 workspace 与 learner 关联数据。两个真实 HTTP 入口的确定性并发测试先复现失败，采用已有 Interaction 使用的一致性只读事务后均通过。

同轮还发现 Book 更新反馈时 `Run.updated_at` 可能不变，已通过反馈指纹修正；下一轮真实豆包验证见上表。

第二次真实复验中，Hint 首发请求使用 learner revision 13，旧 Run 的后台收尾将其推进到 14；恢复 Provider 请求时重建了不同输入，触发不可变请求回执错误。该轮文字 Hint 未通过，不作为最终验收成功样本。

固定上下文后的确定性回归覆盖首次 Hint → Provider PENDING → 旧 Book/learner 完成 → 新 worker 恢复同一 Hint：首次等待 453 SQL / 2.500 秒，恢复 272 SQL / 1.328 秒，ContextBuilder 只调用一次，Provider 只生成一次。普通成功 Hint 为 494 SQL；公开列表仍为 122 SQL。上述为固定 Provider 的数据库集成测试，不代表真实模型总响应时间。

## 自动化验证

- 最终后端聚焦：26 passed，覆盖完整 Hint 流程、Provider pending 后旧 Book 收尾再恢复、真实 Run/Evidence HTTP 并发读取、2/5 轮历史预算和请求内校验缓存。
- 失败后连续 5 轮聊天：全部列表 610 SQL、最新页 509 SQL、单条 507 SQL，历史 Hint 校验恰好 5 次，没有恢复原有递归膨胀。
- Agent 聚焦：56 passed / 43 subtests。
- Voice：12 项通过，包括只读查询的每次 1 SQL、同时间戳反馈变化和连接自动刷新。
- 前端：8 个 Godot 脚本通过。覆盖提前成功后两次提问、Book 与 Hint 并行、恢复后采用 Hint 返回的 Run、来源不符拒绝，以及默认 Hint 1 秒/Run 4 秒、Retry-After 和显式配置。
- 固定时钟下，命令在第 6 秒完成：Hint 第 6 秒读到，普通 Run 第 8 秒读到。
- Ruff、`git diff --check` 和最终测量脚本的 Godot `--check-only` 通过。

## 日志

- [最终阶段汇总](dingdang-freshness-summary.json)
- [最终界面时间线](dingdang-freshness-ui.jsonl)
- [最终 Worker 阶段、SQL 与耗时](dingdang-freshness-worker.stdout.log)
- [最终 HTTP 状态与 SQL](dingdang-freshness-gateway.stdout.log)
- [最终真实模型调用](dingdang-freshness-relay.stdout.log)
- [最终数据库与客户端状态验证](dingdang-freshness-validation.json)
- [真实豆包连接与两次刷新](voice-freshness-live.jsonl)
- [最终轻量查询基准](dingdang-freshness-context-benchmark.json)
- [第一次失败复验](dingdang-freshness-first-run/README.md)
- [第二次失败复验](dingdang-freshness-second-run/README.md)
- [前后端语音接口文档](../agent/docs/DINGDANG_VOICE_FRONTEND_BACKEND_API.md)
- [此前代码提交优化](submit-latency-optimization-2026-09-09.md)

原数据库新增 Run 数为 0；测试只写独立副本。验证结束后已停止测量助手、删除三个本次临时数据库，将 `walnut-play-postgres` 恢复为测试前的停止状态，保留原容器、原数据库与日志文件。
