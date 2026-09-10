# 服务失败提示与后端待处理项

## 问题与结论

2026-09-10，正确代码已通过 Build 和 Activation，随后 Agent Turn 在执行 Skill 前失败。
已保存响应中的请求模型为 `deepseek-v4-flash`，返回模型为 `deepseek-flash`；使用生产校验函数
离线检查该响应，得到 `WorkflowInvariantError: Provider reply authority drifted`。
当天只读访问官方 `/models` 得到 `deepseek-flash`、`deepseek-v4-pro`。这证明当前公开标识变化，
不能仅凭名称证明底层模型版本完全等价。

## 本次前端修改

- 保留已验证 Command 的结构化错误，包括 Build、Activation 和失败 Turn 的恢复链路。
- 桥接层传递错误分类，以及当前源码是否确实已通过认证；每次操作清除过期错误。
- 区分编译错误、运行错误、目标未达成、服务失败、尚无法确认结果。
- 只有已认证源码才显示「代码检查已通过」。轮询超时不声称世界未变化或代码错误。
- 已知服务内部错误提示联系老师检查服务，无需因此反复修改答案。
- 标题悬停可查看格式受限的错误编号。原始消息、任意 details 和富文本不直接展示给学生。
- 保留修改入口、代码与世界投影；进入下一阶段时清理上次错误编号。

## 验证

八项 Godot 回归通过：Build、Activation、Submit/Run、Build feedback 恢复、pending Turn 重启恢复、
Turn 失败状态恢复、Crop bridge、Crop demo。覆盖后端错误保留、已认证源码遇到内部错误、超时
不确定性、编译错误、目标失败、错误编号过滤及修改入口。1280×720 实际渲染检查通过。
本轮未调用模型生成、未修改后端/Agent 源码或数据库，未重启运行服务。

## 仍需后端负责人处理

1. 运行配置：`scripts/start-persistent-play.ps1` 已支持 `-Model`，可用当前模型名称配置新运行。
   必须同步 Gateway、Worker、Relay 的模型配置，不能只替换一个进程的环境变量。
2. 持久化配置：现有 `agent_profiles.profile_json.model_version` 仍是旧名称，且配置有哈希。
   `persistent_play_authority.py` 会校验整个配置及哈希；`turn_worker.py` 会对照 Agent Profile、
   Command 与运行配置。需设计保留学习进度、草稿及历史凭据的数据迁移，不能只改启动参数、
   清库或批量替换历史 Command/响应。
3. 新配置验证：通过模型请求与响应身份校验，验证新提交产生真实 Run、Receipt、世界结果和反馈。
   已终结为 DEAD_LETTER 的旧任务不会因配置修复自动成功，应由用户显式发起新的提交。
4. 可选诊断改善：`adapters/postgres/durable_llm.py` 和 `workers/workflow_worker.py` 可保留安全的
   模型不一致原因及预期/实际标识。当前仅凭通用 INTERNAL_ERROR，前端无法断言具体就是模型配置。

优先尝试配置与受控数据迁移，保留 main 后端源码。若需源码或契约调整，由后端负责人评估；
不删除模型身份校验、不让前端伪造成功、不将上游原始响应改写为旧模型名。
